"""Import 17TRACK backfills and serve them from the main DuckDB snapshot."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

import duckdb
import pandas as pd

from . import core
from .tracking_clock import carrier_wall_time, utc_key
from track17 import C_END_CARRIER_CODES, WYT_CARRIER_CODES, STATUS_CN


FILENAME = "17TRACK历史轨迹.sqlite3"
TABLE = "trk_archive_summary"
EVENT_TABLE = "trk_archive_event"

_TRACKING_ROWS = """
    WITH ranked AS (
        SELECT e.tracking_no, e.carrier_code, e.event_time_utc, e.description, e.location,
               row_number() OVER (
                   PARTITION BY e.tracking_no,e.carrier_code
                   ORDER BY e.event_time_utc DESC NULLS LAST, e.provider_position,e.event_position
               ) AS position,
               max(CASE WHEN e.stage='Delivered' OR e.substatus LIKE 'Delivered_%'
                        THEN e.event_time_utc END)
                   OVER (PARTITION BY e.tracking_no,e.carrier_code) AS delivered_at
        FROM tracking_events e
    )
    SELECT p.tracking_no,p.fulfillment_no,p.state,
           d.carrier_code,d.latest_status,d.latest_substatus,d.event_count,
           r.event_time_utc,r.description,r.location,r.delivered_at,
           coalesce(d.checked_at,p.checked_at) AS checked_at
    FROM packages p
    LEFT JOIN tracking_details d ON d.tracking_no=p.tracking_no AND d.event_count>0
    LEFT JOIN ranked r ON r.tracking_no=d.tracking_no AND r.carrier_code=d.carrier_code AND r.position=1
    WHERE p.state IN ('tracked','empty','registry_unmatched')
"""

_EMPTY = """(SELECT NULL::VARCHAR AS 单号, NULL::VARCHAR AS 履约单号,
                   NULL::VARCHAR AS 回补状态, NULL::INTEGER AS carrier, 0::BIGINT AS 事件数,
                   NULL::TIMESTAMP AS 查询时间 WHERE FALSE)"""


def path_for(facts):
    return Path(facts).with_name(FILENAME)


def _open(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10))


def refresh_archive(con, path):
    """Copy a consistent SQLite snapshot and replace the DuckDB archive atomically."""
    con.execute("BEGIN TRANSACTION")
    try:
        with _open(path) as source:
            source.execute("BEGIN")
            count = _copy_events(con, source)
            frame = pd.read_sql_query(_TRACKING_ROWS, source)
        _refresh_summary(con, frame)
        counts = con.execute(f"""
            SELECT 回补状态, count(*), sum(事件数) FROM {TABLE} GROUP BY 1 ORDER BY 1
        """).fetchall()
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return count, counts


def _copy_events(con, source):
    iso = _iso_projection(source)
    prior = con.execute(f"SELECT count(*) FROM {EVENT_TABLE}").fetchone()[0] if core.columns(con, EVENT_TABLE) else 0
    expected = source.execute("SELECT count(*) FROM tracking_events").fetchone()[0]
    if expected < prior:
        raise ValueError("历史轨迹来源少于当前数据库中的记录，请检查是否使用了完整的回补文件")
    con.execute(f"""
        CREATE OR REPLACE TABLE {EVENT_TABLE} (
            tracking_no VARCHAR NOT NULL, carrier_code INTEGER NOT NULL,
            provider_position INTEGER NOT NULL, event_position INTEGER NOT NULL,
            provider_code INTEGER, provider_name VARCHAR,
            event_time_utc VARCHAR, event_time_raw VARCHAR, event_time_iso VARCHAR,
            stage VARCHAR, substatus VARCHAR, description VARCHAR, location VARCHAR
        )
    """)
    for chunk in pd.read_sql_query(f"""
        SELECT tracking_no,carrier_code,provider_position,event_position,
               provider_code,provider_name,event_time_utc,event_time_raw,{iso} AS event_time_iso,
               stage,substatus,description,location
        FROM tracking_events ORDER BY tracking_no,carrier_code,provider_position,event_position
    """, source, chunksize=100_000):
        con.register("archive_event_batch", chunk)
        try:
            con.execute(f"INSERT INTO {EVENT_TABLE} SELECT * FROM archive_event_batch")
        finally:
            con.unregister("archive_event_batch")
    copied = con.execute(f"SELECT count(*) FROM {EVENT_TABLE}").fetchone()[0]
    if copied != expected:
        raise RuntimeError(f"历史轨迹行数校验失败：预期 {expected}，实际 {copied}")
    con.execute(f"CREATE INDEX ix_trk_archive_event_number ON {EVENT_TABLE}(tracking_no)")
    print(f"历史轨迹逐条入库并核对行数：{copied:,} 条", flush=True)
    return copied


def _refresh_summary(con, frame):
    for key in ("carrier_code", "event_count"):
        frame[key] = frame[key].astype("Int64")
    for key in ("event_time_utc", "delivered_at", "checked_at"):
        frame[key] = pd.to_datetime(frame[key], format="ISO8601", utc=True, errors="coerce").dt.tz_localize(None)
    frame["status_cn"] = frame["latest_status"].map(STATUS_CN)
    con.register("archive_records", frame)
    carriers = C_END_CARRIER_CODES | WYT_CARRIER_CODES
    con.register("archive_carriers", pd.DataFrame(carriers.items(), columns=["name", "code"]))
    try:
        con.execute(f"""
            CREATE OR REPLACE TABLE {TABLE} AS
            WITH matched AS (
                SELECT p.*, count(*) OVER (PARTITION BY p.tracking_no) AS candidates,
                       coalesce(s.carrier,c.code) AS expected
                FROM archive_records p
                JOIN fct_order o ON o.履约单号=p.fulfillment_no AND o.单号归一=p.tracking_no
                LEFT JOIN trk_status s ON s.单号=p.tracking_no
                LEFT JOIN archive_carriers c ON c.name=lower(trim(o.C端承运商))
                WHERE lower(coalesce(o.物流商,''))<>'postnet'
            )
            SELECT tracking_no AS 单号, fulfillment_no AS 履约单号, state AS 回补状态,
                   carrier_code::INTEGER AS carrier, coalesce(event_count,0) AS 事件数,
                   latest_status AS 状态, status_cn AS 状态中文, latest_substatus AS 子状态,
                   event_time_utc AS 最新事件时间, description AS 最新描述, location AS 地点,
                   delivered_at AS 签收时间, checked_at + INTERVAL '8 hours' AS 查询时间,
                   latest_status='Delivered' AS 是否签收
            FROM matched WHERE candidates=1 OR carrier_code=expected
        """)
    finally:
        con.unregister("archive_records")
        con.unregister("archive_carriers")


def sources(con, waybills=None):
    """Return the status, missing-registration, and archive summaries used by the web app."""
    columns = core.columns(con, "trk_status")
    missing_clocks = [f"NULL::VARCHAR AS {name}" for name in ("最新事件当地时间", "签收当地时间")
                      if name not in columns]
    native = f"(SELECT *, {', '.join(missing_clocks)} FROM trk_status)" if missing_clocks else "trk_status"
    missing_native = "trk_missing"
    archived = TABLE
    if waybills:
        condition = f"单号 IN ({waybills})"
        native = f"(SELECT * FROM {native} WHERE {condition})"
        missing_native = f"(SELECT 单号 FROM trk_missing WHERE {condition})"
        archived = f"(SELECT * FROM {TABLE} WHERE {condition})"
    if not core.columns(con, TABLE):
        return native, missing_native, _EMPTY
    status = f"""(SELECT * FROM {native} n WHERE NOT EXISTS (
        SELECT 1 FROM {archived} h WHERE h.单号=n.单号 AND h.回补状态='tracked'
          AND (n.最新事件时间 IS NULL OR h.最新事件时间>=n.最新事件时间))
        UNION ALL BY NAME
        SELECT 单号, carrier, 状态, 状态中文, 子状态, 最新事件时间, 最新描述, 地点,
               是否签收, 签收时间, 查询时间
        FROM {archived} h WHERE h.回补状态='tracked' AND NOT EXISTS (
            SELECT 1 FROM {native} n WHERE n.单号=h.单号
              AND n.最新事件时间 IS NOT NULL
              AND (h.最新事件时间 IS NULL OR n.最新事件时间>h.最新事件时间)))"""
    missing = f"""(SELECT 单号 FROM {missing_native}
                  UNION SELECT 单号 FROM {archived} WHERE 回补状态='registry_unmatched')"""
    return status, missing, archived


def events(con, number, carrier):
    if carrier is None or not core.columns(con, EVENT_TABLE):
        return []
    result = con.execute(f"""
        SELECT event_time_utc AS 事件时间, stage AS 主状态, substatus AS 子状态,
               description AS 描述, location AS 地点, provider_name AS 事件承运商,
               event_time_raw AS 原始时间, event_time_iso AS iso
        FROM {EVENT_TABLE} WHERE tracking_no=? AND carrier_code=?
        ORDER BY event_time_utc ASC NULLS LAST, provider_position,event_position DESC
    """, [number, carrier])
    columns = [column[0] for column in result.description]
    records = [dict(zip(columns, row)) for row in result.fetchall()]
    return [{**record, "当地时间": carrier_wall_time(record["原始时间"], record["iso"])}
            for record in records]


def _iso_projection(source):
    fields = {column[1] for column in source.execute("PRAGMA table_info(tracking_events)")}
    return "event_time_iso" if "event_time_iso" in fields else "NULL"


def fill_local_times(con, rows):
    """Fill summary clocks using an event for the same waybill, carrier, and UTC instant."""
    fields = (("最新事件时间", "最新事件当地时间"), ("承运商签收时间", "签收当地时间"))
    pending = {}
    for row in rows:
        number, carrier = row.get("单号归一"), row.get("回补运输商代码")
        if not number or pd.isna(carrier):
            continue
        for internal, display in fields:
            timestamp = utc_key(row.get(internal))
            if timestamp and (pd.isna(row.get(display)) or not row.get(display)):
                pending.setdefault((number, int(carrier), timestamp), []).append((row, display))
    if not pending or not core.columns(con, EVENT_TABLE):
        return
    numbers = sorted({number for number, _, _ in pending})
    markers = ",".join("?" for _ in numbers)
    for number, carrier, timestamp, raw, local_iso in con.execute(f"""
            SELECT tracking_no,carrier_code,event_time_utc,event_time_raw,event_time_iso
            FROM {EVENT_TABLE} WHERE tracking_no IN ({markers})
            ORDER BY provider_position,event_position
        """, numbers).fetchall():
        key = (number, carrier, timestamp)
        if key in pending and (wall_time := carrier_wall_time(raw, local_iso)):
            for row, display in pending.pop(key):
                row[display] = wall_time


def main():
    parser = argparse.ArgumentParser(description="将 17TRACK 历史轨迹和索引导入履约数据库")
    parser.add_argument("--facts", type=Path, default=core.DUCK_DB)
    args = parser.parse_args()
    if not args.facts.is_file():
        parser.error("履约数据库不存在")
    with closing(duckdb.connect(str(args.facts))) as con:
        _, summaries = refresh_archive(con, path_for(args.facts))
        from . import analytics_snapshot, track_lookup
        track_lookup.build(con)
        analytics_snapshot.build(con)
        for state, packages, count in summaries:
            print(f"{state}: {packages:,} 个运单，{count:,} 条事件")


if __name__ == "__main__":
    main()
