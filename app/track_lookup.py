"""One current tracking row per fulfillment, refreshed with the DuckDB snapshot."""
from . import core, tracking_archive, tracking_freshness


TABLE = "fct_track_lookup"
LABELS = {
    "carrier_conflict": "承运商待核对",
    "signed_time_invalid": "WMS 签收时间异常",
    "unmatched": "API 账号登记未匹配",
    "queried_empty": "已查询 · 未返回轨迹",
    "signed_unstored": "WMS 已签收 · 待核实",
    "shipped_unstored": "已出库 · 待同步轨迹",
    "other_unstored": "其他履约单 · 无本地结果",
}


def projection(con):
    tracking, missing, archive = tracking_archive.sources(con)
    fields = core.columns(con, "trk_event")
    checks = tracking_freshness.TABLE
    if not core.columns(con, checks):
        checks = "(SELECT NULL::VARCHAR AS number, NULL::VARCHAR AS outcome WHERE FALSE)"
    abnormal = 'o.WMS签收异常' if 'WMS签收异常' in core.columns(con, 'fct_order') else 'FALSE'
    if "运输商代码" in fields:
        counts = "SELECT 单号,运输商代码,count(*) AS n FROM trk_event GROUP BY 1,2"
        event_join = """LEFT JOIN native_counts e ON e.单号=o.单号归一 AND e.运输商代码=coalesce(t.carrier,0)
                        LEFT JOIN native_counts u ON u.单号=o.单号归一 AND u.运输商代码 IS NULL"""
        event_count = "coalesce(e.n,0)+coalesce(u.n,0)"
    elif fields:
        counts = "SELECT 单号,count(*) AS n FROM trk_event GROUP BY 1"
        event_join = "LEFT JOIN native_counts e ON e.单号=o.单号归一"
        event_count = "coalesce(e.n,0)"
    else:
        counts = "SELECT NULL::VARCHAR AS 单号,0::BIGINT AS n WHERE FALSE"
        event_join = ""
        event_count = "0"
    labels = " ".join(f"WHEN '{key}' THEN '{label}'" for key,label in LABELS.items())
    return f"""
        WITH native_counts AS ({counts}), merged AS (
            SELECT o.履约单号,t.单号,t.carrier,t.状态,t.状态中文,t.子状态,t.签收时间,t.签收当地时间,
                   t.最新事件时间,t.最新事件当地时间,t.最新描述,t.地点,t.停滞天数,
                   h.carrier AS 回补运输商代码,h.回补状态,
                   coalesce(t.查询时间,h.查询时间) AS 查询时间,
                   v.outcome AS 核对结果,
                   t.单号 IS NULL AND m.单号 IS NOT NULL AS 未注册,
                   coalesce(nullif(h.事件数,0),{event_count}) AS 逐条事件数,
                   CASE WHEN v.outcome='carrier_conflict' AND coalesce(nullif(h.事件数,0),{event_count})=0 THEN 'carrier_conflict'
                        WHEN t.单号 IS NOT NULL THEN 'tracked'
                        WHEN v.outcome='carrier_conflict' THEN 'carrier_conflict'
                        WHEN {abnormal} THEN 'signed_time_invalid'
                        WHEN m.单号 IS NOT NULL THEN 'unmatched'
                        WHEN h.回补状态='empty' THEN 'queried_empty'
                        WHEN o.已签收=1 THEN 'signed_unstored'
                        WHEN o.签出时间 IS NOT NULL THEN 'shipped_unstored'
                        ELSE 'other_unstored' END AS 覆盖
            FROM fct_order o LEFT JOIN {tracking} t ON t.单号=o.单号归一
            LEFT JOIN {missing} m ON m.单号=o.单号归一
            LEFT JOIN {archive} h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
            LEFT JOIN {checks} v ON v.number=o.单号归一
            {event_join}
            QUALIFY row_number() OVER (PARTITION BY o.履约单号 ORDER BY t.最新事件时间 DESC NULLS LAST,
                (h.carrier IS NOT DISTINCT FROM t.carrier) DESC,h.事件数 DESC NULLS LAST,t.carrier)=1
        )
        SELECT * REPLACE (
            CASE WHEN 覆盖='carrier_conflict' THEN NULL ELSE 状态 END AS 状态,
            CASE WHEN 覆盖='carrier_conflict' THEN NULL ELSE 状态中文 END AS 状态中文,
            CASE WHEN 覆盖='carrier_conflict' THEN NULL ELSE 子状态 END AS 子状态),
            CASE WHEN 覆盖='carrier_conflict' THEN '{LABELS['carrier_conflict']}'
                 ELSE coalesce(状态中文,CASE 覆盖 {labels} ELSE '17TRACK 状态未提供' END) END AS 状态标签
        FROM merged
    """


def source(con):
    return TABLE if core.columns(con, TABLE) else f"({projection(con)})"


def build(con):
    con.execute(f"CREATE OR REPLACE TABLE {TABLE} AS {projection(con)}")
    if con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] != con.execute("SELECT count(*) FROM fct_order").fetchone()[0]:
        raise RuntimeError("轨迹筛选表行数与履约单不一致")
    con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_track_lookup_order ON {TABLE}(履约单号)")
