-- ============================================================
-- 04 看板汇总层 (mart)
-- 所有前端/BI 工具只读这一层, 不再直接碰明细
-- ============================================================

-- ---------- KPI 卡片 ----------
CREATE SCHEMA IF NOT EXISTS mart;

CREATE OR REPLACE TABLE mart.kpi AS
WITH f AS (SELECT * FROM dw.fct_shipment),
p AS (SELECT * FROM dw.cfg_params),
win AS (
    SELECT
        (SELECT 数据截止日 FROM p)                              AS 数据截止日,
        (SELECT max(创建日期) FROM f)                           AS 最新下单日,
        (SELECT 最新下单日 - INTERVAL 6 DAY FROM p)             AS 近7天起,
        (SELECT 最新下单日 - INTERVAL 29 DAY FROM p)            AS 近30天起
),
agg AS (
    SELECT
        -- 规模
        count(*)                                                              AS 累计履约单,
        count(*) FILTER (WHERE 创建日期 >= (SELECT 近7天起 FROM win))         AS 近7天单量,
        round(count(*) FILTER (WHERE 创建日期 >= (SELECT 近7天起 FROM win)) / 7.0, 0) AS 近7天日均单量,
        sum(件数)                                                             AS 累计件数,
        -- 时效
        round(quantile_cont(仓内_小时, 0.5) FILTER (WHERE 仓内_小时 > 0), 1)  AS 仓内P50h,
        round(quantile_cont(仓内_小时, 0.9) FILTER (WHERE 仓内_小时 > 0), 1)  AS 仓内P90h,
        round(quantile_cont(尾程_天, 0.5) FILTER (WHERE 尾程_天 > 0), 2)      AS 尾程P50d,
        round(quantile_cont(尾程_天, 0.9) FILTER (WHERE 尾程_天 > 0), 2)      AS 尾程P90d,
        -- 达标率 (仅在观察窗完整的单上算)
        round(100.0 * count(*) FILTER (WHERE 时效状态 = '已签收_达标')
              / nullif(count(*) FILTER (WHERE 时效状态 IN ('已签收_达标','已签收_超时','在途超期未达')), 0), 2) AS 全链达标率Pct,
        round(100.0 * count(*) FILTER (WHERE 尾程达标)
              / nullif(count(*) FILTER (WHERE 尾程达标 IS NOT NULL OR 时效状态 = '在途超期未达'), 0), 2)        AS 尾程达标率Pct,
        round(100.0 * count(*) FILTER (WHERE 仓内达标)
              / nullif(count(*) FILTER (WHERE 仓内达标 IS NOT NULL), 0), 2)                                    AS 仓内达标率Pct,
        -- 在途与风险
        count(*) FILTER (WHERE 时效状态 IN ('在途未超期','在途超期未达'))      AS 在途单量,
        count(*) FILTER (WHERE 时效状态 = '在途超期未达')                      AS 超期未达单量,
        count(*) FILTER (WHERE 时效状态 = '仓内超期未发')                      AS 仓内超期未发,
        count(*) FILTER (WHERE 时效状态 = '仓内处理中')                        AS 仓内处理中,
        -- 数据质量
        round(100.0 * count(*) FILTER (WHERE 缺快递单号) / count(*), 2)        AS 缺单号率Pct,
        round(100.0 * count(*) FILTER (WHERE 状态异常) / count(*), 2)          AS 状态异常率Pct,
        count(*) FILTER (WHERE 时间倒挂)                                       AS 时间倒挂单数,
        count(*) FILTER (WHERE 状态 = '已取消')                                AS 已取消单数
    FROM f
)
SELECT * FROM agg;

-- ---------- 日趋势 ----------
CREATE OR REPLACE TABLE mart.daily AS
SELECT
    创建日期,
    count(*)                                                        AS 单量,
    sum(件数)                                                       AS 件数,
    round(sum(申报金额), 2)                                         AS 申报金额,
    round(quantile_cont(仓内_小时, 0.5) FILTER (WHERE 仓内_小时 > 0), 1) AS 仓内P50h,
    round(quantile_cont(仓内_小时, 0.9) FILTER (WHERE 仓内_小时 > 0), 1) AS 仓内P90h,
    round(quantile_cont(尾程_天, 0.5) FILTER (WHERE 尾程_天 > 0), 2)     AS 尾程P50d,
    round(quantile_cont(尾程_天, 0.9) FILTER (WHERE 尾程_天 > 0), 2)     AS 尾程P90d,
    round(100.0 * count(*) FILTER (WHERE 时效状态 = '已签收_达标')
          / nullif(count(*) FILTER (WHERE 时效状态 IN ('已签收_达标','已签收_超时','在途超期未达')), 0), 2) AS 全链达标率Pct,
    count(*) FILTER (WHERE 时效状态 = '已签收_达标')                 AS 已签达标,
    count(*) FILTER (WHERE 时效状态 = '已签收_超时')                 AS 已签超时,
    count(*) FILTER (WHERE 时效状态 = '在途超期未达')                AS 在途超期,
    count(*) FILTER (WHERE 时效状态 IN ('在途未超期','在途超期未达')) AS 在途
FROM dw.fct_shipment
GROUP BY 1 ORDER BY 1;

-- ---------- 线路 (仓库 x 承运商 x 国家) ----------
CREATE OR REPLACE TABLE mart.lane AS
SELECT
    仓库代码, 物流服务商, 目的国,
    count(*)                                                              AS 单量,
    sum(件数)                                                             AS 件数,
    round(avg(计费重g) FILTER (WHERE 计费重g > 0), 0)                     AS 平均计费重g,
    round(avg(申报金额), 2)                                               AS 平均申报金额,
    round(quantile_cont(仓内_小时, 0.5) FILTER (WHERE 仓内_小时 > 0), 1)  AS 仓内P50h,
    round(quantile_cont(尾程_天, 0.5) FILTER (WHERE 尾程_天 > 0), 2)      AS 尾程P50d,
    round(quantile_cont(尾程_天, 0.9) FILTER (WHERE 尾程_天 > 0), 2)      AS 尾程P90d,
    any_value(仓内目标h)                                                  AS 仓内目标h,
    any_value(尾程目标d)                                                  AS 尾程目标d,
    round(100.0 * count(*) FILTER (WHERE 时效状态 = '已签收_达标')
          / nullif(count(*) FILTER (WHERE 时效状态 IN ('已签收_达标','已签收_超时','在途超期未达')), 0), 2) AS 全链达标率Pct,
    count(*) FILTER (WHERE 时效状态 = '在途超期未达')                     AS 超期未达,
    count(*) FILTER (WHERE 时效状态 IN ('在途未超期','在途超期未达'))      AS 在途,
    count(*) FILTER (WHERE 缺快递单号)                                    AS 缺单号,
    round(100.0 * count(*) FILTER (WHERE 缺快递单号) / count(*), 2)       AS 缺单号率Pct
FROM dw.fct_shipment
GROUP BY 1, 2, 3
HAVING count(*) >= 50
ORDER BY 单量 DESC;

-- ---------- 承运商 ----------
CREATE OR REPLACE TABLE mart.carrier AS
SELECT
    物流服务商,
    count(*)                                                              AS 单量,
    round(100.0 * count(*) / sum(count(*)) OVER (), 2)                    AS 单量占比Pct,
    count(DISTINCT 目的国)                                                AS 覆盖国家数,
    round(quantile_cont(仓内_小时, 0.5) FILTER (WHERE 仓内_小时 > 0), 1)  AS 仓内P50h,
    round(quantile_cont(尾程_天, 0.5) FILTER (WHERE 尾程_天 > 0), 2)      AS 尾程P50d,
    round(quantile_cont(尾程_天, 0.9) FILTER (WHERE 尾程_天 > 0), 2)      AS 尾程P90d,
    round(100.0 * count(*) FILTER (WHERE 时效状态 = '已签收_达标')
          / nullif(count(*) FILTER (WHERE 时效状态 IN ('已签收_达标','已签收_超时','在途超期未达')), 0), 2) AS 全链达标率Pct,
    count(*) FILTER (WHERE 时效状态 IN ('在途未超期','在途超期未达'))      AS 在途,
    count(*) FILTER (WHERE 时效状态 = '在途超期未达')                     AS 超期未达,
    count(*) FILTER (WHERE 状态异常)                                      AS 状态异常单,
    count(*) FILTER (WHERE 缺快递单号)                                    AS 缺单号
FROM dw.fct_shipment
GROUP BY 1
HAVING count(*) >= 10
ORDER BY 单量 DESC;

-- ---------- 仓库 ----------
CREATE OR REPLACE TABLE mart.warehouse AS
SELECT
    仓库代码,
    count(*)                                                              AS 单量,
    round(quantile_cont(仓内_小时, 0.5) FILTER (WHERE 仓内_小时 > 0), 1)  AS 仓内P50h,
    round(quantile_cont(仓内_小时, 0.9) FILTER (WHERE 仓内_小时 > 0), 1)  AS 仓内P90h,
    any_value(仓内目标h)                                                  AS 仓内目标h,
    round(100.0 * count(*) FILTER (WHERE 仓内达标) / nullif(count(*) FILTER (WHERE 仓内达标 IS NOT NULL), 0), 2) AS 仓内达标率Pct,
    count(*) FILTER (WHERE 时效状态 = '仓内超期未发')                      AS 仓内超期未发,
    round(100.0 * count(*) FILTER (WHERE 缺快递单号) / count(*), 2)       AS 缺单号率Pct
FROM dw.fct_shipment
GROUP BY 1
HAVING count(*) >= 100
ORDER BY 单量 DESC;

-- ---------- 在途库龄 ----------
CREATE OR REPLACE TABLE mart.intransit_age AS
SELECT
    物流服务商,
    目的国,
    count(*) FILTER (WHERE 库龄天 BETWEEN 0 AND 3)    AS 龄_0_3天,
    count(*) FILTER (WHERE 库龄天 BETWEEN 4 AND 7)    AS 龄_4_7天,
    count(*) FILTER (WHERE 库龄天 BETWEEN 8 AND 14)   AS 龄_8_14天,
    count(*) FILTER (WHERE 库龄天 BETWEEN 15 AND 30)  AS 龄_15_30天,
    count(*) FILTER (WHERE 库龄天 > 30)               AS 龄_30天以上,
    count(*)                                          AS 在途合计,
    round(avg(库龄天), 1)                             AS 平均库龄天,
    max(库龄天)                                       AS 最大库龄天
FROM (
    SELECT 物流服务商, 目的国,
           date_diff('day', coalesce(签出时间_ts::DATE, 创建日期), 数据截止日) AS 库龄天
    FROM dw.fct_shipment
    WHERE 时效状态 IN ('在途未超期', '在途超期未达')
)
GROUP BY 1, 2
HAVING count(*) >= 20
ORDER BY 在途合计 DESC;

-- ---------- 异常清单汇总 ----------
CREATE OR REPLACE TABLE mart.exception AS
SELECT
    仓库代码, 物流服务商, 目的国, 时效状态, 状态,
    count(*)                       AS 单量,
    round(sum(申报金额), 2)        AS 申报金额,
    min(coalesce(签出时间_ts::DATE, 创建日期)) AS 最早单日期,
    round(avg(观察天数), 0)        AS 平均已观察天
FROM dw.fct_shipment
WHERE 时效状态 IN ('在途超期未达', '仓内超期未发') OR 状态异常 OR 缺快递单号
GROUP BY 1, 2, 3, 4, 5
HAVING count(*) >= 5
ORDER BY 单量 DESC;

-- ---------- 数据健康 ----------
CREATE OR REPLACE TABLE mart.health AS
SELECT '日期覆盖' AS 检查项,
       (SELECT count(DISTINCT 创建日期) FROM dw.fct_shipment)::VARCHAR || ' 天 (2026-01-01~2026-11-12 应有 316 天)' AS 结果,
       '缺 01-28~02-23 (25天)、03-31~04-23 (22天), 趋势图需断点显示' AS 说明
UNION ALL SELECT '数据新鲜度',
       (SELECT 数据截止日::VARCHAR FROM dw.cfg_params) || ' (距今 ' ||
       (SELECT date_diff('day', 数据截止日, current_date) FROM dw.cfg_params)::VARCHAR || ' 天)',
       '看板头部必须显式展示, 否则会误读"今日单量为0"'
UNION ALL SELECT '时间戳脏值',
       (SELECT '签收最晚录到 ' || 最新签收日原始::VARCHAR FROM dw.cfg_params) || ' / 已剔除 ' ||
       (SELECT count(*) FROM dw.fct_shipment WHERE 时长异常)::VARCHAR || ' 单',
       '4 行签收时间被录成 2028-04-22; 若直接取 max 会把数据截止日拉到 2028, 全部在途单误判超期'
UNION ALL SELECT '缺快递单号率',
       (SELECT round(100.0 * count(*) FILTER (WHERE 缺快递单号) / count(*), 2) FROM dw.fct_shipment)::VARCHAR || ' %',
       '其中已取消单占多数, 属正常; 非取消单缺号需排查'
UNION ALL SELECT '时间倒挂',
       (SELECT count(*) FROM dw.fct_shipment WHERE 时间倒挂)::VARCHAR || ' 单',
       '签收早于签出, 需从时效分母剔除'
UNION ALL SELECT '单号可用性',
       (SELECT count(DISTINCT 单号)::VARCHAR || ' 个单号可提交(覆盖 ' ||
               count(DISTINCT 履约单号)::VARCHAR || ' 单)'
        FROM dw.stg_track_leg WHERE 单号质量 = 'ok'),
       '一个履约单最多 3 条腿(主单号/转单号/头程单号), 故单号数 > 单数'
UNION ALL SELECT '高风险单号',
       (SELECT count(DISTINCT 单号)::VARCHAR || ' 个 GFUS/SPX 内部号'
        FROM dw.stg_track_leg WHERE 单号质量 = 'ok' AND 格式说明 LIKE '%内部号%'),
       '万邑通内部号大概率 17track 查不到, 需回落到转单号(USPS 段)'
UNION ALL SELECT '仓内分段缺失',
       '分拣/复核/打包 时间覆盖率仅 25%~16%',
       '仅能算 创建->签出 整段, 做不了仓内工序级拆解';
