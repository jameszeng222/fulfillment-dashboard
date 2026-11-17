-- ============================================================
-- 02 单号归一化 + 承运商映射 (17track 接入的前置依赖)
--
-- 核心问题: 库里的"快递单号"并不是一个可直接查询的单号, 而是三条腿的混合:
--   头程单号 (1Z... UPS)  ->  快递单号 (货代主单, 如 3PE / GFUS / SPX)  ->  转单号 (末端 USPS 887...)
-- WYT-USPS 占 72% 的单量里, 41.9% 是 GFUS 开头的万邑通内部号, 很可能查不到;
-- postnet 的 15 万个单号只对应 3 万个不同的号 (5 倍复用)。
-- 所以必须先建映射, 再谈调 API, 否则 17track 会大量返回"未找到"。
-- ============================================================

-- 单号标准化: 去空格/短横, 统一大写
CREATE OR REPLACE MACRO dw_norm(s) AS
    upper(regexp_replace(coalesce(s, ''), '[^A-Za-z0-9]', '', 'g'));

-- 承运商识别规则表 (可维护, 上线后按 17track 实测结果调整)
CREATE OR REPLACE TABLE dw.dim_track_rule (
    优先级      INTEGER,
    正则        VARCHAR,
    建议承运商  VARCHAR,   -- 17track carrier code; auto = 交给 17track 自动识别
    说明        VARCHAR,
    提交模式    VARCHAR    -- forced / auto
);

INSERT INTO dw.dim_track_rule VALUES
 (10, '^1Z[A-Z0-9]{16}$',        'ups',        'UPS 1Z 标准号',           'forced'),
 (11, '^9[0-9]{19,21}$',         'usps',       'USPS IMpb 9x 开头',       'forced'),
 (12, '^[0-9]{20,22}$',          'usps',       'USPS 长数字',             'forced'),
 (13, '^YT[0-9]{16}$',           'yunexpress', '云途 YT',                 'forced'),
 (14, '^GF[A-Z]{2}[0-9]+$',      'auto',       '万邑通 GFUS 内部号',      'auto'),
 (15, '^SPX[A-Z][0-9]+$',        'auto',       'SPX 段号',                'auto'),
 (16, '^3PE[0-9]+$',             'auto',       '3PE 货代主单',            'auto'),
 (17, '^PPA[0-9]+$',             'auto',       'postnet 南非',            'auto'),
 (18, '^D[0-9]{12}$',            'auto',       'postnet 南非 D 段',       'auto'),
 (19, '^XSSY[0-9]+$',            'auto',       'XS Express',              'auto'),
 (20, '^[0-9]{12}$',             'auto',       '12 位纯数字(多为末端)',   'auto'),
 (21, '^[0-9]{10,11}$',          'auto',       '10-11 位数字(DHL 段)',    'auto'),
 (22, '^[A-Z]{2,5}[0-9]{6,}$',   'auto',       '字母前缀 + 数字',         'auto'),
 (99, '^.+$',                    'auto',       '未识别格式',              'auto');

-- 单号级明细: 每行 = 一个待查单号 (一个履约单最多 3 条腿)
CREATE OR REPLACE TABLE dw.stg_track_leg AS
WITH s AS (
    SELECT 履约单号, 出库单号, 仓库代码, 物流服务商, 目的国, 状态,
           创建日期, 签出时间_ts, 签收时间_ts,
           快递单号_raw, 转单号_raw, 头程运单号_raw
    FROM dw.stg_shipment
),
leg_raw AS (
    SELECT 履约单号, 出库单号, 仓库代码, 物流服务商, 目的国, 状态,
           创建日期, 签出时间_ts, 签收时间_ts, '主单号' AS 腿, 快递单号_raw AS 单号原文, 1 AS 腿序
    FROM s WHERE 快递单号_raw IS NOT NULL
    UNION ALL
    SELECT 履约单号, 出库单号, 仓库代码, 物流服务商, 目的国, 状态,
           创建日期, 签出时间_ts, 签收时间_ts, '转单号', 转单号_raw, 2
    FROM s WHERE 转单号_raw IS NOT NULL
    UNION ALL
    SELECT 履约单号, 出库单号, 仓库代码, 物流服务商, 目的国, 状态,
           创建日期, 签出时间_ts, 签收时间_ts, '头程单号', 头程运单号_raw, 3
    FROM s WHERE 头程运单号_raw IS NOT NULL
),
dedup AS (   -- 三条腿重复同一个号时只保留优先级最高的一条
    SELECT *, dw_norm(单号原文) AS 单号,
           row_number() OVER (PARTITION BY 履约单号, dw_norm(单号原文) ORDER BY 腿序) AS rn
    FROM leg_raw
),
norm AS (
    SELECT * FROM dedup WHERE rn = 1 AND length(单号) >= 8
)
SELECT
    n.履约单号, n.出库单号, n.仓库代码, n.物流服务商, n.目的国, n.状态,
    n.创建日期, n.签出时间_ts, n.签收时间_ts,
    n.腿, n.单号, n.单号原文,
    r.建议承运商 AS 建议承运商,
    r.提交模式   AS 提交模式,
    r.说明       AS 格式说明,
    CASE
        WHEN n.单号原文 ~ '\+'                 THEN 'hold'    -- "9865361214+1" 这种多包裹写法无法直接查
        WHEN upper(n.单号) IN ('CANCELLED','NULL','NA','NONE','0','-') THEN 'bad'
        WHEN length(n.单号) < 8                THEN 'bad'
        ELSE 'ok'
    END AS 单号质量
FROM norm n
LEFT JOIN LATERAL (
    SELECT 建议承运商, 提交模式, 说明
    FROM dw.dim_track_rule r
    WHERE regexp_matches(n.单号, r.正则)
    ORDER BY 优先级
    LIMIT 1
) r ON TRUE;

-- 履约单号 -> 优先跟踪单号 (给 17track 提交用)
CREATE OR REPLACE VIEW dw.v_track_priority AS
SELECT
    履约单号,
    any_value(出库单号)      AS 出库单号,
    any_value(仓库代码)      AS 仓库代码,
    any_value(物流服务商)    AS 物流服务商,
    any_value(目的国)        AS 目的国,
    any_value(状态)          AS 状态,
    any_value(签出时间_ts)   AS 签出时间_ts,
    any_value(签收时间_ts)   AS 签收时间_ts,
    -- 主查单号: 优先末端转单号(能拿到真实签收), 其次货代主单
    arg_min(单号, CASE 腿 WHEN '转单号' THEN 1 WHEN '主单号' THEN 2 ELSE 3 END)
        FILTER (WHERE 单号质量 = 'ok') AS 主查单号,
    arg_min(建议承运商, CASE 腿 WHEN '转单号' THEN 1 WHEN '主单号' THEN 2 ELSE 3 END)
        FILTER (WHERE 单号质量 = 'ok') AS 主查承运商,
    bool_or(腿 = '转单号' AND 单号质量 = 'ok') AS 有末端可查号,
    count(*) FILTER (WHERE 单号质量 = 'ok')   AS 可查单号数,
    count(*) FILTER (WHERE 单号质量 = 'hold') AS 待人工单号数,
    count(*) FILTER (WHERE 单号质量 = 'bad')  AS 废弃单号数
FROM dw.stg_track_leg
GROUP BY 履约单号;
