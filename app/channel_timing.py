"""Timing bases and the earliest recorded transport scan from local tracking stores."""
from contextlib import closing
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Literal

import duckdb
import pandas as pd

from . import core


TimingBasis = Literal["paid", "created", "shipped", "online"]
ONLINE_START = "CASE WHEN scan.start_at>=o.创建时间 THEN scan.start_at END"
_archive_cache_lock = Lock()


def timing_start(basis, with_scans, online_start=ONLINE_START):
    online = f"WHEN 'online' THEN {online_start}" if with_scans else ""
    return f"""CASE coalesce({basis},'shipped')
               WHEN 'paid' THEN o.支付时间
               WHEN 'shipped' THEN o.签出时间
               {online} END"""


def workday_minutes(start, end):
    """Elapsed weekday minutes between two timestamps already in Beijing time."""
    day_us = 86_400_000_000

    def within_week(timestamp):
        weekday = f"isodow({timestamp})"
        return f"""(least({weekday}-1,5)*{day_us} +
                   CASE WHEN {weekday}<=5 THEN
                       date_diff('microsecond',date_trunc('day',{timestamp}),{timestamp})
                   ELSE 0 END)"""

    full_weeks = f"date_diff('week',date_trunc('week',{start}),date_trunc('week',{end}))"
    return f"(({full_weeks}*{5 * day_us} + {within_week(end)} - {within_week(start)}) / 60000000.0)"


_TRANSPORT_STAGES = "'PickedUp','Departure','Arrival'"
_TRANSPORT_SUBSTATUSES = "'InTransit_PickedUp','InTransit_Departure','InTransit_Arrival'," \
    "'InTransit_CustomsProcessing','InTransit_CustomsReleased','InTransit_Other'"


def _transport_scan(stage, substatus, description):
    return f"""({stage} IN ({_TRANSPORT_STAGES}) OR {substatus} IN ({_TRANSPORT_SUBSTATUSES}))
        AND lower(coalesce({description},'')) NOT LIKE '%awaiting item%'
        AND lower(coalesce({description},'')) NOT LIKE '%label created%'
        AND lower(coalesce({description},'')) NOT LIKE '%information sent%'
        AND lower(coalesce({description},'')) NOT LIKE '%information received%'
        AND lower(coalesce({description},'')) NOT LIKE '%electronic information submitted%'"""


@lru_cache(maxsize=1)
def _archive_scans(path, version):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as archive:
        if not archive.execute("SELECT 1 FROM sqlite_master WHERE name='tracking_events'").fetchone():
            return None
        scans = pd.read_sql_query(f"""
            SELECT tracking_no, min(event_time_utc) AS scan_utc
            FROM tracking_events
            WHERE event_time_utc IS NOT NULL AND {_transport_scan('stage', 'substatus', 'description')}
            GROUP BY tracking_no HAVING count(DISTINCT carrier_code)=1
        """, archive)
    scans["scan_utc"] = pd.to_datetime(
        scans["scan_utc"], format="ISO8601", utc=True, errors="coerce"
    ).dt.tz_localize(None)
    return scans


def _archive_version(path):
    stat = path.stat()
    try:
        wal = path.with_name(path.name + "-wal").stat()
        journal = (wal.st_size, wal.st_mtime_ns) if wal.st_size else ()
    except FileNotFoundError:
        journal = ()
    return hashlib.sha256(repr((1, stat.st_ino, stat.st_size, stat.st_mtime_ns, journal)).encode()).hexdigest()


def transport_archive_version(facts_path=None):
    archive = Path(facts_path or core.DUCK_DB).with_name("17TRACK历史轨迹.sqlite3")
    try:
        return _archive_version(archive)
    except FileNotFoundError:
        return None


def _archive_transport_source(con, archive):
    """Reuse the exact SQLite aggregate across preview restarts."""
    cache = archive.with_name(".17track-transport-scans.parquet")
    version = _archive_version(archive)
    with _archive_cache_lock:
        if cache.is_file():
            try:
                stored = con.execute("""
                    SELECT decode(value) FROM parquet_kv_metadata(?) WHERE decode(key)='archive_version'
                """, [str(cache)]).fetchone()
                if stored == (version,):
                    path = cache.as_posix().replace("'", "''")
                    return f"SELECT tracking_no, scan_utc FROM read_parquet('{path}')"
            except duckdb.Error:
                pass
        scans = _archive_scans(archive, version)
        if scans is None:
            return None
        con.register("archive_transport_scans", scans)
        if _archive_version(archive) == version:
            temporary = None
            try:
                with NamedTemporaryFile(dir=archive.parent, suffix=".parquet", delete=False) as file:
                    temporary = Path(file.name)
                con.execute(f"""
                    COPY archive_transport_scans TO ?
                    (FORMAT PARQUET, KV_METADATA {{ archive_version: '{version}' }})
                """, [str(temporary)])
                os.replace(temporary, cache)
            except (OSError, duckdb.Error):
                # Read-only installations can still use the in-process aggregate.
                pass
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return "SELECT tracking_no, scan_utc FROM archive_transport_scans"


def online_scan_join(con, waybill="o.单号归一"):
    """Match by normalized waybill. Return a single China-time scan per waybill."""
    sources = []
    if core.columns(con, "trk_milestone"):
        sources.append(f"""
            SELECT 单号 AS tracking_no, 时间 AS scan_utc FROM trk_milestone
            WHERE 时间 IS NOT NULL AND 节点 IN ({_TRANSPORT_STAGES})
        """)
    if core.columns(con, "trk_event"):
        sources.append(f"""
            SELECT 单号 AS tracking_no, 事件时间 AS scan_utc FROM trk_event
            WHERE 事件时间 IS NOT NULL AND {_transport_scan('主状态', '子状态', '描述')}
        """)
    db_path = con.execute("SELECT path FROM duckdb_databases() WHERE database_name=current_database()").fetchall()[0][0]
    archive = Path(db_path or core.DUCK_DB).with_name("17TRACK历史轨迹.sqlite3")
    if archive.is_file():
        source = _archive_transport_source(con, archive)
        if source:
            sources.append(source)
    if not sources:
        return "LEFT JOIN (SELECT NULL::TIMESTAMP AS start_at) scan ON false"
    return f"""LEFT JOIN (
        SELECT tracking_no, min(scan_utc)::TIMESTAMP + INTERVAL '8 hours' AS start_at
        FROM ({" UNION ALL ".join(sources)}) scans GROUP BY tracking_no
    ) scan ON scan.tracking_no={waybill}"""
