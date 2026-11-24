"""Reusable per-fulfillment analytics facts, rebuilt atomically with tracking data."""
from . import core
from .carrier_delivery import reporting_facts
from .channel_timing import ONLINE_START, online_scan_join, transport_archive_version
from .order_time import delivery_facts
from .regional_pickup import build_pickups


TABLE = "fct_analytics"
SPLIT_TABLE = "fct_analytics_order_groups"
INFO = "fct_analytics_info"


def available(con):
    return bool(core.columns(con, TABLE))


def facts(con):
    if available(con):
        return TABLE
    return reporting_facts(con, delivery_facts(con, core.active_facts(core.order_facts(con))))


def scan_source(con, archive_version):
    if available(con) and core.columns(con, INFO):
        version = con.execute(f"SELECT archive_version FROM {INFO}").fetchall()
        if version == [(archive_version,)]:
            return "", "CASE WHEN o.analytics_online>=o.创建时间 THEN o.analytics_online END"
    return online_scan_join(con), ONLINE_START


def build(con):
    from .us_analysis import US_COUNTRIES, UNKNOWN, geography_sql

    active = delivery_facts(con, core.active_facts(core.order_facts(con)))
    measured = reporting_facts(con, active)
    con.execute(f"CREATE TEMP TABLE analytics_orders AS SELECT * FROM {measured}")
    build_pickups(con, "analytics_orders")
    con.execute(f"""CREATE TEMP TABLE analytics_us_orders AS
        SELECT 履约单号 FROM analytics_orders WHERE lower(trim(目的国)) IN {US_COUNTRIES}""")
    geo, params = geography_sql(con, "analytics_us_orders")
    con.execute(f"CREATE TEMP TABLE analytics_geography AS {geo}", params)
    scans = online_scan_join(con)
    con.execute(f"""
        CREATE OR REPLACE TABLE {TABLE} AS
        SELECT o.*, p.pickup AS analytics_pickup, scan.start_at AS analytics_online,
               coalesce(g.geo_state,'') AS analytics_state, coalesce(g.geo_region,'{UNKNOWN}') AS analytics_region
        FROM analytics_orders o LEFT JOIN regional_pickups p USING (履约单号)
        LEFT JOIN analytics_geography g USING (履约单号) {scans}
    """)
    if con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] != con.execute("SELECT count(*) FROM analytics_orders").fetchone()[0]:
        raise RuntimeError("统计明细数量与有效履约单不一致")
    con.execute(f"""
        CREATE OR REPLACE TABLE {SPLIT_TABLE} AS
        WITH grouped AS (
            SELECT coalesce(trim(平台),'') AS platform, trim(订单号) AS order_no,
                   count(*) AS fulfillments, count(nullif(trim(仓库),'')) AS known_warehouses,
                   min(nullif(upper(trim(仓库)),'')) AS first_warehouse,
                   max(nullif(upper(trim(仓库)),'')) AS last_warehouse
            FROM analytics_orders WHERE nullif(trim(订单号),'') IS NOT NULL GROUP BY 1,2
        ) SELECT platform,order_no,CASE WHEN fulfillments=1 THEN 'single'
             WHEN first_warehouse<>last_warehouse THEN 'cross'
             WHEN known_warehouses=fulfillments THEN 'same' ELSE 'unknown' END AS kind FROM grouped
    """)
    con.execute(f"CREATE OR REPLACE TABLE {INFO} (archive_version VARCHAR)")
    db_path = con.execute("SELECT path FROM duckdb_databases() WHERE database_name=current_database()").fetchone()[0]
    con.execute(f"INSERT INTO {INFO} VALUES (?)", [transport_archive_version(db_path)])
    print(f"统计明细：{con.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0]:,} 单", flush=True)
