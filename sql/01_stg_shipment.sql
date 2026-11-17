-- ============================================================
-- 01 履约单级宽表 (L1 staging)
-- 粒度: 1 行 = 1 履约单号
-- 关键事实: 履约单号 : 出库单号 : 快递单号 = 1:1:1, SKU 行平均 1.425 行/单
-- ============================================================
CREATE SCHEMA IF NOT EXISTS dw;

CREATE OR REPLACE TABLE dw.stg_shipment AS
SELECT
    履约单号,
    any_value(出库单号)                               AS 出库单号,
    any_value(订单号)                                 AS 订单号,
    any_value(平台订单号)                             AS 平台订单号,
    any_value(平台)                                   AS 平台,
    any_value(仓库代码)                               AS 仓库代码,
    any_value(目的国家英文名称)                       AS 目的国,
    any_value(目的国家中文名称)                       AS 目的国中文,
    any_value(物流服务商)                             AS 物流服务商,
    any_value("C端显示物流服务商名称")                AS 前端承运商,
    any_value(运输方式)                               AS 运输方式,
    any_value(出库单状态)                             AS 状态,
    any_value("履约单创建类型（订单履约/人工创建）")   AS 创建类型,
    any_value(订单物流服务等级)                       AS 服务等级,
    any_value(履约单创建原因)                         AS 创建原因,
    any_value(物流渠道选择方式)                       AS 选渠道方式,
    any_value("省、州")                               AS 省州,
    any_value(城市)                                   AS 城市,
    any_value(邮编)                                   AS 邮编,

    -- 三条腿的单号（原始值，清洗在 02）
    any_value(nullif(trim(快递单号), ''))             AS 快递单号_raw,
    any_value(nullif(trim(转单号), ''))               AS 转单号_raw,
    any_value(nullif(trim(头程运单号), ''))           AS 头程运单号_raw,

    -- 时间轴
    min(创建时间_ts)                                  AS 创建时间_ts,
    min(支付时间_ts)                                  AS 支付时间_ts,
    min(签出时间_ts)                                  AS 签出时间_ts,
    min(签收时间_ts)                                  AS 签收时间_ts,

    -- 商品与金额
    count(*)                                          AS sku行数,
    count(DISTINCT sku)                               AS sku种数,
    sum(数量)                                         AS 件数,
    sum(申报金额)                                     AS 申报金额,
    max(预估计费重)                                   AS 计费重g,   -- 订单级字段被复制到每行, 取 max 不可 sum
    max("签出重/g")                                   AS 签出重g,

    any_value(创建日期)                               AS 创建日期,
    any_value(创建月份)                               AS 创建月份
FROM 履约单明细
GROUP BY 履约单号;

-- 数据截止日: 取"最晚的合理业务事件", 不能直接用 max()。
-- 库里存在录入错值(4 行签收时间被录成 2028-04-22), 直接取 max 会把截止日拉到 2028,
-- 导致所有在途单都被算成超期。这里用 下单日 / 签出日 取大, 并给 30 天容忍窗。
CREATE OR REPLACE TABLE dw.cfg_params AS
WITH raw AS (
    SELECT max(创建日期)                                   AS 最新下单日,
           max(签出时间_ts)::DATE                          AS 最新签出日,
           max(签收时间_ts)::DATE                          AS 最新签收日原始
    FROM dw.stg_shipment
)
SELECT
    greatest(最新下单日, coalesce(最新签出日, 最新下单日)) AS 数据截止日,
    最新下单日,
    最新签出日,
    最新签收日原始,
    CASE WHEN 最新签收日原始 > coalesce(最新签出日, 最新下单日) + INTERVAL 30 DAY
         THEN '已剔除 ' || 最新签收日原始::VARCHAR || ' 的录入异常值'
         ELSE '无异常' END AS 截止日修正说明,
    current_timestamp AS 生成时间
FROM raw;
