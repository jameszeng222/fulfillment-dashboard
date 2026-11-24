"""Stage local control-tower updates before replacing the live snapshot."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil

import duckdb

import alerts
import build_orders
import ingest
import track17
from app import core, rules, tracking_archive, tracking_freshness, tracking_sync, track_lookup
from app.tracking_clock import ensure_display_columns
from backfill_tracking import REGISTRY_BATCH, ReadOnlyClient, RetryableRequestError, chunks, fetch_batch


INPUT = Path.home() / "Desktop" / "履约单2026"
ERP_JOB = "履约单滚动导入"
TRACK_LIMIT = 4000
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS sync_ledger (
    sha256 VARCHAR PRIMARY KEY,
    filename VARCHAR,
    size_bytes BIGINT,
    rows BIGINT,
    orders BIGINT,
    创建日起 DATE,
    创建日止 DATE,
    batch VARCHAR,
    ingested_at TIMESTAMP
)
"""


def file_stamp(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def export_digest(path):
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def new_exports(con, folder):
    seen = set()
    if core.columns(con, "sync_ledger"):
        seen.update(row[0] for row in con.execute("SELECT sha256 FROM sync_ledger").fetchall())
    exports = []
    for path in sorted(folder.glob("履约单数据_*.xls*")):
        if not path.is_file() or path.suffix.lower() not in ingest.SUPPORTED:
            continue
        try:
            stamp = file_stamp(path)
            digest = export_digest(path)
            if stamp != file_stamp(path) or digest in seen:
                continue
        except OSError:
            continue
        seen.add(digest)
        exports.append((path, stamp, digest))
    return exports


def import_exports(con, exports):
    if exports:
        con.execute(LEDGER_DDL)
    imported = []
    for path, stamp, digest in exports:
        print(f"导入 {path.name}", flush=True)
        rows = ingest.read_one(path)
        if file_stamp(path) != stamp or export_digest(path) != digest:
            raise RuntimeError(f"文件仍在更新，下次重试：{path.name}")
        stats = ingest.ingest_df(con, rows, batch=path.stem, mode="upsert", keep_newer=True)
        con.execute("""
            INSERT INTO sync_ledger
                (sha256,filename,size_bytes,rows,orders,创建日起,创建日止,batch,ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [digest, path.name, stamp[0], stats["rows"], stats["orders"],
              stats["dmin"], stats["dmax"], stats["batch"], datetime.now()])
        imported.append(path.name)
    return imported


def refresh_tracks(con, candidates, token):
    client = ReadOnlyClient(token, requests_per_second=2.8)
    by_number = {row[0]: row[1:] for row in candidates}
    totals = Counter()

    def read(numbers):
        try:
            return fetch_batch(client, numbers)
        except RetryableRequestError:
            return numbers, (), {}, {}, None

    with ThreadPoolExecutor(max_workers=4) as workers:
        for numbers, registered, found, failed, pending in workers.map(read, chunks(list(by_number), REGISTRY_BATCH)):
            codes = defaultdict(set)
            for number, code in registered:
                codes[number].add(code)
            checked = datetime.fromisoformat(core.now_iso()).replace(tzinfo=None)
            checks = []
            for number in numbers:
                carrier, last_mile, previous, last_event, last_status, conflict = by_number[number]
                primary, alternate = track17.carrier_lookup(carrier, last_mile, previous, last_event is not None)
                registered_codes = codes[number]
                chosen = next((code for code in (primary, alternate if last_event is None else None)
                               if code in registered_codes), None)
                if not primary and len(registered_codes) == 1:
                    chosen = next(iter(registered_codes))
                carrier_code = chosen or primary
                error = failed.get((number, chosen))
                result = found.get((number, chosen))
                if conflict:
                    outcome = "carrier_conflict"
                elif pending is None:
                    outcome = "read_error"
                elif not registered_codes:
                    outcome = "unmatched"
                    if last_event is None:
                        con.execute("""
                            INSERT INTO trk_missing (单号,carrier,首次发现,末次检查,检查次数)
                            VALUES (?,?,?,?,1) ON CONFLICT (单号) DO UPDATE SET
                                末次检查=excluded.末次检查, 检查次数=trk_missing.检查次数+1
                        """, [number, primary or 0, checked, checked])
                elif chosen is None:
                    outcome = "carrier_conflict"
                elif result is None:
                    outcome = "carrier_conflict" if error == -18019910 else "read_error"
                else:
                    info = result.get("track_info") or {}
                    event_time = track17.tracking_utc(info.get("latest_event") or {})
                    latest = event_time.replace(tzinfo=None) if event_time else None
                    next_status = (info.get("latest_status") or {}).get("status")
                    has_events = any(group.get("events") for group in (info.get("tracking") or {}).get("providers") or [])
                    if last_event is not None and (latest is None or latest < last_event or (
                        latest == last_event and last_status == "Delivered" and next_status != "Delivered"
                    )):
                        outcome = "older"
                    else:
                        track17.save_one(con, result)
                        totals["accepted"] += 1
                        if has_events:
                            totals["with_events"] += 1
                        outcome = "verified" if has_events and latest is not None else "empty"
                totals[outcome] += 1
                registration = ",".join(map(str, sorted(registered_codes))) if pending is not None else None
                checks.append((number, carrier_code, checked, checked if outcome == "verified" else None, outcome, error, registration))
            tracking_freshness.save_checks(con, checks)

    return {"queried": len(by_number), **{name: totals[name] for name in (
        "accepted", "with_events", "verified", "unmatched", "empty", "read_error", "carrier_conflict", "older"
    )}}


def apply_pushes(con, pushes):
    from datetime import timedelta

    live_status, _, _ = tracking_archive.sources(con)
    numbers = [push["number"] for push in pushes]
    facts = con.execute(f"""
        SELECT o.单号归一,o.物流商,o.C端承运商,t.carrier,t.最新事件时间,t.状态,{core.active_order()} AS active,
               coalesce(o.签出时间 < {tracking_freshness.OLDEST_DISPATCH}, false) AS too_old
        FROM fct_order o LEFT JOIN {live_status} t ON t.单号=o.单号归一
        WHERE o.单号归一 IN (SELECT unnest(?::VARCHAR[]))
        QUALIFY row_number() OVER (PARTITION BY o.单号归一
            ORDER BY active DESC,o.签出时间 DESC NULLS LAST,o.创建日期 DESC,o.履约单号)=1
    """, [numbers]).fetchall()
    matched = {row[0]: row[1:] for row in facts}
    now = datetime.fromisoformat(core.now_iso())
    outcomes = []
    for push in pushes:
        push_id, number, carrier = push["id"], push["number"], push["carrier"]
        if number not in matched:
            expired = now - datetime.fromisoformat(push["received_at"]) >= timedelta(days=1)
            outcomes.append([push_id, "ignored" if expired else "queued", "unmatched"])
            continue
        raw_carrier, lastmile, previous, last_event, last_status, active, too_old = matched[number]
        if not active:
            outcomes.append([push_id, "ignored", "not_in_scope"])
            continue
        if too_old:
            outcomes.append([push_id, "ignored", "outside_tracking_window"])
            continue
        if any((name or "").strip().casefold() == "postnet" for name in (raw_carrier, lastmile)):
            outcomes.append([push_id, "ignored", "postnet"])
            continue
        if previous and last_event and carrier != previous:
            outcomes.append([push_id, "ignored", "different_carrier"])
            continue
        result = json.loads(push["payload"])
        info = result["track_info"]
        latest = track17.tracking_utc(info["latest_event"])
        latest = latest.replace(tzinfo=None) if latest else None
        next_status = info["latest_status"].get("status")
        if last_event and (not latest or latest < last_event or (
            latest == last_event and last_status == "Delivered" and next_status != "Delivered"
        )):
            outcomes.append([push_id, "ignored", "older"])
            continue
        track17.save_one(con, result)
        matched[number] = (raw_carrier, lastmile, carrier, latest, next_status, active, too_old)
        outcomes.append([push_id, "applied", None])
    return outcomes


def prepare(stage, folder, track_limit=None, import_archive=False, repair=False):
    source = core.DUCK_DB
    started = core.now_iso()
    token = os.environ.get("TRACK17_TOKEN", "").strip()
    if track_limit is not None and not token:
        raise RuntimeError("未找到本机 17TRACK 凭据")
    pushes = tracking_sync.ready_pushes() if track_limit is None else []
    with closing(core.duck()) as con:
        exports = new_exports(con, folder) if track_limit is None else []
        candidates = tracking_freshness.candidates(con, track_limit, repair=repair) if track_limit is not None else []
        if not exports and not candidates and not pushes and not import_archive and not repair:
            return 3
        if source.with_suffix(source.suffix + ".wal").exists():
            raise RuntimeError("履约数据库正在写入，下次重试")
        original_stamp = file_stamp(source)
        shutil.copyfile(source, stage)
    with closing(duckdb.connect(str(stage))) as con:
        tracking = None
        imported = import_exports(con, exports)
        if imported or repair or "WMS签收异常" not in core.columns(con, "fct_order"):
            build_orders.build(con)
        archive_count = None
        if import_archive:
            archive_count, _ = tracking_archive.refresh_archive(con, tracking_archive.path_for(source))
        if candidates:
            ensure_display_columns(con)
            con.execute("BEGIN TRANSACTION")
            tracking = refresh_tracks(con, candidates, token)
            con.execute("COMMIT")
            print("17TRACK：", tracking, flush=True)
        outcomes = []
        if pushes:
            ensure_display_columns(con)
            con.execute("BEGIN TRANSACTION")
            outcomes = apply_pushes(con, pushes)
            con.execute("COMMIT")
            if not imported and not import_archive and not repair and not any(row[1] == "applied" for row in outcomes):
                with core.ops() as ops:
                    tracking_sync.finish_pushes(ops, outcomes)
                return 3
        track_lookup.build(con)
        con.execute("CHECKPOINT")
    stage.with_suffix(".json").write_text(json.dumps({
        "source_stamp": original_stamp, "started": started, "exports": imported,
        "tracking": tracking, "pushes": outcomes, "archive_count": archive_count,
    }, ensure_ascii=False), encoding="utf-8")
    return 0


def publish(stage, run_id=None):
    payload = json.loads(stage.with_suffix(".json").read_text(encoding="utf-8"))
    source = core.DUCK_DB
    previous = source.with_name(source.stem + ".previous.duckdb")
    moved = False
    if file_stamp(source) != payload["source_stamp"] or source.with_suffix(source.suffix + ".wal").exists():
        raise RuntimeError("准备期间事实库已更新，下次重新生成快照")
    try:
        with core.ops() as ops:
            ops.execute("BEGIN IMMEDIATE")
            with closing(duckdb.connect(str(stage))) as con:
                alerts.calculate(con, None, *rules.load_settings(ops))
                con.execute("CREATE OR REPLACE TABLE fct_alert AS SELECT * FROM computed_alerts")
                alerts.write_order_alert(con)
                alerts.update_alert_mirror(con, ops)
                con.execute("CHECKPOINT")
            if payload.get("pushes"):
                tracking_sync.finish_pushes(ops, payload["pushes"])
            names = list(payload["exports"])
            jobs = []
            if names:
                jobs.append((ERP_JOB, names.copy()))
            if payload.get("archive_count") is not None:
                archive_job = "17TRACK 历史轨迹导入"
                jobs.append((archive_job, {"events": payload["archive_count"]}))
                names.append(archive_job)
            if payload["tracking"] is not None:
                if run_id is None:
                    jobs.append((tracking_sync.POLL_JOB, payload["tracking"]))
                else:
                    tracking_sync.finish_poll(ops, run_id, "ok", payload["tracking"])
                names.append(tracking_sync.POLL_JOB)
            push_count = sum(row[1] == "applied" for row in payload.get("pushes", []))
            if push_count:
                jobs.append((tracking_sync.PUSH_JOB, {"applied": push_count}))
                names.append(tracking_sync.PUSH_JOB)
            ops.executemany("""
                INSERT INTO job_runs(job,started_at,finished_at,status,detail) VALUES(?,?,?,'ok',?)
            """, [(job, payload["started"], core.now_iso(), json.dumps(detail, ensure_ascii=False))
                  for job, detail in jobs])
            os.replace(source, previous)
            moved = True
            os.replace(stage, source)
    except Exception:
        if moved:
            os.replace(previous, source)
        raise
    print("已更新履约监控系统：" + "、".join(names), flush=True)
    return 0


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=["prepare", "publish", "poll-start", "poll-idle", "poll-failed"])
    cli.add_argument("--stage", type=Path)
    cli.add_argument("--run-id", type=int)
    cli.add_argument("--reason", choices=["credentials", "prepare", "publish", "interrupted"])
    cli.add_argument("--folder", type=Path, default=INPUT)
    cli.add_argument("--track-limit", type=int, help="查询近期运单的已登记轨迹，每次最多 4,000 个")
    cli.add_argument("--import-archive", action="store_true", help="复制历史轨迹到快照，不调用 17TRACK")
    cli.add_argument("--repair", action="store_true", help="重建签收统计和轨迹筛选表，并仅重查承运商匹配异常")
    args = cli.parse_args()
    if args.action == "poll-start":
        print(tracking_sync.begin_poll())
        return 0
    if args.action in {"poll-idle", "poll-failed"}:
        if args.run_id is None:
            cli.error("需要同步运行编号")
        with core.ops() as ops:
            status = "idle" if args.action == "poll-idle" else "failed"
            tracking_sync.finish_poll(ops, args.run_id, status, {"reason": args.reason} if args.reason else None)
        return 0
    if args.stage is None:
        cli.error("需要临时快照路径")
    if args.stage.resolve().parent != core.DUCK_DB.resolve().parent or args.stage.resolve() == core.DUCK_DB.resolve():
        cli.error("临时快照必须位于履约监控系统项目目录")
    if args.action == "prepare" and (args.stage.exists() or args.stage.with_suffix(".json").exists()):
        cli.error("临时快照路径已存在")
    if args.track_limit is not None and not 1 <= args.track_limit <= TRACK_LIMIT:
        cli.error("轨迹批量限制必须在 1–4,000 之间")
    if args.import_archive and (args.action != "prepare" or args.track_limit is not None):
        cli.error("历史轨迹导入须单独准备，不与 17TRACK 查询同批运行")
    if args.action == "prepare":
        return prepare(args.stage, args.folder, args.track_limit, args.import_archive, args.repair)
    return publish(args.stage, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
