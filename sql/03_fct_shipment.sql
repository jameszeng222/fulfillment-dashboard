-- ============================================================
-- 03 时效事实表 (L3)
--
-- 口径设计要点:
-- 1) 三个时钟分开: 支付->创建(系统), 创建->签出(仓内, 可控), 签出->签收(尾程, 承运商可控)
-- 2) 右删失: 近 X 天下的单还没签收是正常的, 不能拉低签收率。
--    用"观察窗口"判定: 观察天数 >= 目标天数 才计入超期分母。
-- 3) 已取消单 (2.9%) 不计入时效分母。
-- 4) 签收时间来自 WMS 状态流转(已完成), 不是承运商事件 -> 后续用 17track 做对账。
-- ============================================================

-- 线路时效基准: 取近 180 天历史 P90 并向上取整, 作为初始目标; 业务可改
CREATE OR REPLACE TABLE dw.dim_lane_sla AS
WITH wh AS (
    SELECT 仓库代码, 物流服务商, 目的国,
           quantile_cont(date_diff('minute', 创建时间_ts, 签出时间_ts) / 60.0, 0.9) AS p90_仓内h,
           count(*) AS n_仓内
    FROM dw.stg_shipment
    WHERE 签出时间_ts > 创建时间_ts AND 创建日期 >= DATE '2026-05-16'
    GROUP BY 1, 2, 3
),
lm AS (
    SELECT 仓库代码, 物流服务商, 目的国,
           quantile_cont(date_diff('minute', 签出时间_ts, 签收时间_ts) / 1440.0, 0.9) AS p90_尾程d,
           count(*) AS n_尾程
    FROM dw.stg_shipment
    WHERE 签收时间_ts > 签出时间_ts AND 创建日期 >= DATE '2026-05-16'
    GROUP BY 1, 2, 3
),
lane AS (
    SELECT coalesce(wh.仓库代码, lm.仓库代码) AS 仓库代码,
           coalesce(wh.物流服务商, lm.物流服务商) AS 物流服务商,
           coalesce(wh.目的国, lm.目的国) AS 目的国,
           round(least(greatest(coalesce(wh.p90_仓内h, 48), 12), 120)) AS 仓内目标h,
           round(least(greatest(coalesce(lm.p90_尾程d, 8), 2), 30))     AS 尾程目标d,
           coalesce(wh.n_仓内, 0) AS 样本_仓内,
           coalesce(lm.n_尾程, 0) AS 样本_尾程
    FROM wh FULL OUTER JOIN lm
      ON wh.仓库代码 = lm.仓库代码 AND wh.物流服务商 = lm.物流服务商 AND wh.目的国 = lm.目的国
    WHERE coalesce(wh.n_仓内, 0) + coalesce(lm.n_尾程, 0) >= 300
)
SELECT 仓库代码, 物流服务商, 目的国, 仓内目标h, 尾程目标d, 样本_仓内, 样本_尾程 FROM lane
UNION ALL
-- 兜底层级, 供三级回退匹配
SELECT 仓库代码, 物流服务商, '*', median(仓内目标h), median(尾程目标d), sum(样本_仓内), sum(样本_尾程)
FROM lane GROUP BY 1, 2
UNION ALL
SELECT 仓库代码, '*', '*', median(仓内目标h), median(尾程目标d), sum(样本_仓内), sum(样本_尾程)
FROM lane GROUP BY 1
UNION ALL
SELECT '*', '*', '*', 48, 8, 0, 0;

CREATE OR REPLACE TABLE dw.fct_shipment AS
WITH p AS (SELECT * FROM dw.cfg_params),
s AS (
    SELECT st.*, p.数据截止日
    FROM dw.stg_shipment st CROSS JOIN p
),
sla AS (
    SELECT s.履约单号,
           coalesce(l1.仓内目标h, l2.仓内目标h, l3.仓内目标h, l4.仓内目标h, 48) AS 仓内目标h,
           coalesce(l1.尾程目标d, l2.尾程目标d, l3.尾程目标d, l4.尾程目标d, 8)  AS 尾程目标d
    FROM s
    LEFT JOIN dw.dim_lane_sla l1 ON l1.仓库代码 = s.仓库代码 AND l1.物流服务商 = s.物流服务商 AND l1.目的国 = s.目的国
    LEFT JOIN dw.dim_lane_sla l2 ON l2.仓库代码 = s.仓库代码 AND l2.物流服务商 = s.物流服务商 AND l2.目的国 = '*'
    LEFT JOIN dw.dim_lane_sla l3 ON l3.仓库代码 = s.仓库代码 AND l3.物流服务商 = '*'      AND l3.目的国 = '*'
    LEFT JOIN dw.dim_lane_sla l4 ON l4.仓库代码 = '*'        AND l4.物流服务商 = '*'      AND l4.目的国 = '*'
)
SELECT
    s.履约单号, s.出库单号, s.订单号, s.平台订单号, s.平台,
    s.仓库代码, s.目的国, s.目的国中文, s.物流服务商, s.运输方式,
    s.状态, s.创建类型, s.服务等级, s.省州, s.城市, s.邮编,
    s.创建日期, s.创建月份, s.数据截止日,

    s.创建时间_ts, s.支付时间_ts, s.签出时间_ts, s.签收时间_ts,

    s.sku行数, s.sku种数, s.件数, s.申报金额, s.计费重g,
    s.快递单号_raw, s.转单号_raw,

    sla.仓内目标h, sla.尾程目标d,

    -- ---------- 分段耗时 ----------
    -- 超过 60 天的"尾程时长"视为录入错误(库里 26 行, 最长 605 天), 置空不参与均值,
    -- 但判定超时时仍用原始时间戳比较, 不让脏数据变成"按时"。
    date_diff('minute', s.支付时间_ts, s.创建时间_ts)                        AS 支付到创建_分钟,
    date_diff('minute', s.创建时间_ts, s.签出时间_ts)                        AS 仓内_分钟,
    CASE WHEN date_diff('minute', s.签出时间_ts, s.签收时间_ts) BETWEEN 0 AND 86400 * 60
         THEN date_diff('minute', s.签出时间_ts, s.签收时间_ts) END          AS 尾程_分钟,
    date_diff('minute', s.创建时间_ts, s.签收时间_ts)                        AS 全链_分钟,

    round(date_diff('minute', s.创建时间_ts, s.签出时间_ts) / 60.0, 2)       AS 仓内_小时,
    CASE WHEN date_diff('minute', s.签出时间_ts, s.签收时间_ts) BETWEEN 0 AND 86400 * 60
         THEN round(date_diff('minute', s.签出时间_ts, s.签收时间_ts) / 1440.0, 3) END AS 尾程_天,
    CASE WHEN date_diff('minute', s.创建时间_ts, s.签收时间_ts) BETWEEN 0 AND 86400 * 60
         THEN round(date_diff('minute', s.创建时间_ts, s.签收时间_ts) / 1440.0, 3) END AS 全链_天,

    -- ---------- 观察窗口 ----------
    date_diff('day', s.创建日期, s.数据截止日)                               AS 观察天数,

    -- ---------- 达标判定 ----------
    CASE WHEN s.签出时间_ts IS NOT NULL
         THEN date_diff('minute', s.创建时间_ts, s.签出时间_ts) / 60.0 <= sla.仓内目标h
    END AS 仓内达标,
    CASE WHEN s.签收时间_ts IS NOT NULL
         THEN date_diff('minute', s.签出时间_ts, s.签收时间_ts) / 1440.0 <= sla.尾程目标d
    END AS 尾程达标,
    CASE WHEN s.签收时间_ts IS NOT NULL
         THEN (date_diff('minute', s.创建时间_ts, s.签出时间_ts) / 60.0 <= sla.仓内目标h)
          AND (date_diff('minute', s.签出时间_ts, s.签收时间_ts) / 1440.0 <= sla.尾程目标d)
    END AS 全链达标,

    -- ---------- 时效状态机 (解决右删失) ----------
    CASE
        WHEN s.状态 = '已取消'                                THEN '已取消'
        WHEN s.签收时间_ts IS NOT NULL AND
             date_diff('minute', s.签出时间_ts, s.签收时间_ts) / 1440.0 <= sla.尾程目标d THEN '已签收_达标'
        WHEN s.签收时间_ts IS NOT NULL                        THEN '已签收_超时'
        WHEN s.签出时间_ts IS NULL AND
             date_diff('day', s.创建日期, s.数据截止日) > sla.仓内目标h / 24.0 THEN '仓内超期未发'
        WHEN s.签出时间_ts IS NULL                            THEN '仓内处理中'
        WHEN date_diff('day', s.签出时间_ts::DATE, s.数据截止日) >= sla.尾程目标d THEN '在途超期未达'
        ELSE '在途未超期'
    END AS 时效状态,

    -- ---------- 异常标记 ----------
    (coalesce(trim(s.快递单号_raw), '') = '')                       AS 缺快递单号,
    (s.签收时间_ts < s.签出时间_ts)                                  AS 时间倒挂,
    (s.签收时间_ts IS NOT NULL AND s.签出时间_ts IS NOT NULL
     AND date_diff('day', s.签出时间_ts, s.签收时间_ts) > 60)        AS 时长异常,
    (s.状态 IN ('物流异常', '仓库异常', '预报失败'))                  AS 状态异常,
    (s.状态 = '待收货')                                             AS 待收货,
    -- 承运商侧是否已确认 (WMS 有签收但承运商未知 -> 待 17track 对账)
    CASE WHEN s.签收时间_ts IS NOT NULL THEN 'WMS已签收_待承运商确认' ELSE '在途' END AS 对账状态
FROM s
JOIN sla ON sla.履约单号 = s.履约单号;

CREATE OR REPLACE VIEW dw.v_有效单 AS
SELECT * FROM dw.fct_shipment WHERE 状态 <> '已取消';
