"""
把 SKU 级的 履约单明细 物化成订单级的 fct_order，供 API 分页查询。

明细是 190 万行（SKU 级），订单是 133 万行。API 每次请求都 GROUP BY 全表扛不住，
所以管道运行时物化一次，加索引，查询只走 fct_order。

用法：
    python build_orders.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
from app.order_time import build_delivery_clock
from app.waybill import key_sql, shipped_waybill_sql

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"

DDL = """
CREATE OR REPLACE TABLE fct_order AS
SELECT
    履约单号,
    any_value(出库单号)                     AS 出库单号,
    {waybill}                              AS 快递单号,
    {waybill_key} AS 单号归一,
    any_value(平台)                         AS 平台,
    any_value(仓库代码)                     AS 仓库,
    any_value(物流服务商)                   AS 物流商,
    any_value("C端显示物流服务商名称")      AS C端承运商,
    {current_channel}                     AS 当前渠道,
    any_value(目的国家英文名称)             AS 目的国,
    any_value(目的国家中文名称)             AS 目的国中文,
    any_value(出库单状态)                   AS 状态,
    any_value(订单号)                       AS 订单号,
    any_value(平台订单号)                   AS 平台订单号,
    min(创建时间_ts)                        AS 创建时间,
    min(支付时间_ts)                        AS 支付时间,
    min(签出时间_ts)                        AS 签出时间,
    coalesce(max(clock.signed_local), max(签收时间_ts)) AS 签收时间,
    max(clock.signed_beijing)               AS 签收北京时间,
    any_value(clock.zone)                   AS 签收时区,
    min(创建日期)                           AS 创建日期,
    any_value(创建月份)                     AS 创建月份,
    sum(数量)::BIGINT                       AS 件数,
    count(DISTINCT sku)::BIGINT             AS sku数,
    count(*)::BIGINT                        AS 明细行数,
    date_diff('minute', min(创建时间_ts), min(签出时间_ts))  AS 仓内分钟,
    date_diff('minute', min(签出时间_ts), max(clock.signed_beijing)) AS 物流分钟,
    max(CASE WHEN coalesce(clock.signed_local,签收时间_ts) IS NOT NULL THEN 1 ELSE 0 END) AS 已签收
FROM 履约单明细
LEFT JOIN fct_delivery_clock clock USING (履约单号)
GROUP BY 履约单号
"""


def build(con):
    columns = {row[0] for row in con.execute("DESCRIBE 履约单明细").fetchall()}
    channel = "max(nullif(trim(当前渠道), ''))" if "当前渠道" in columns else "NULL::VARCHAR"
    con.execute("BEGIN TRANSACTION")
    try:
        build_delivery_clock(con)
        waybill = shipped_waybill_sql()
        con.execute(DDL.format(current_channel=channel, waybill=waybill, waybill_key=key_sql(waybill)))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    n = con.execute("SELECT count(*) FROM fct_order").fetchone()[0]
    print(f"fct_order: {n:,} 行")

    # 常用筛选列建索引，API 分页快
    for col in ("创建日期", "创建月份", "仓库", "平台", "物流商", "目的国", "状态", "单号归一"):
        con.execute(f'CREATE INDEX IF NOT EXISTS ix_order_{col} ON fct_order("{col}")')

    # 详情页要按履约单号取单
    con.execute("CREATE INDEX IF NOT EXISTS ix_order_no ON fct_order(履约单号)")

    # 明细行（详情页展示 SKU）
    con.execute("""
    CREATE OR REPLACE VIEW v_order_sku AS
    SELECT 履约单号, sku, any_value(商品名称) 商品名称, sum(数量)::BIGINT 数量,
           any_value(批次码) 批次码, any_value(产品类目（二级类目）) 类目
    FROM 履约单明细 GROUP BY 履约单号, sku
    """)

    # 订单 + 轨迹 + 预警 的宽视图
    con.execute("""
    CREATE OR REPLACE VIEW v_order_track AS
    SELECT o.履约单号, o.出库单号, o.快递单号, o.平台, o.仓库, o.物流商, o.C端承运商,
           o.目的国, o.目的国中文, o.状态, o.创建时间, o.签出时间, o.签收时间,
           o.仓内分钟, o.物流分钟, o.已签收,
           t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
           t.最新事件时间, t.最新描述, t.地点, t.签收时间 AS 承运商签收时间,
           t.运输天数, t.停滞天数,
           m.单号 IS NOT NULL AS 未注册
    FROM fct_order o
    LEFT JOIN trk_status t ON t.单号 = o.单号归一
    LEFT JOIN trk_missing m ON m.单号 = o.单号归一
    """)

    stat = con.execute("""
        SELECT count(*) FILTER (WHERE 已签收=1) 已签收,
               count(*) FILTER (WHERE 单号归一 IN (SELECT 单号 FROM trk_status)) 有轨迹,
               count(*) FILTER (WHERE 单号归一 IN (SELECT 单号 FROM trk_missing)) 未注册
        FROM fct_order""").fetchone()
    print(f"  已签收 {stat[0]:,}  有轨迹 {stat[1]:,}  未注册 {stat[2]:,}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB))
    try:
        build(con)
        print("完成。")
    finally:
        con.close()
    sys.exit(0)
