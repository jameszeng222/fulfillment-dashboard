"""Durable 17TRACK full pulls and dispatch of the local batch runner."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from . import core


JOB = "17TRACK 全量拉取"
ACTIVE = ("queued", "running")
COUNTERS = ("processed", "with_events", "unmatched", "empty", "read_error", "carrier_conflict", "older", "skipped")
INTERVAL = 8
log = logging.getLogger(__name__)


def latest(db):
    row = db.execute("""
        SELECT id, started_at, finished_at, status, detail FROM job_runs
        WHERE job=? ORDER BY id DESC LIMIT 1
    """, (JOB,)).fetchone()
    return {**dict(row), "detail": json.loads(row["detail"] or "{}")} if row else None


def enqueue(actor):
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        job = latest(db)
        if job and job["status"] in ACTIVE:
            return job
        if job and job["status"] == "failed" and job["detail"].get("total") is not None:
            detail = job["detail"]
            detail.pop("reason", None)
            db.execute("UPDATE job_runs SET status='queued', finished_at=NULL, detail=? WHERE id=?",
                       (json.dumps(detail), job["id"]))
        else:
            db.execute("INSERT INTO job_runs(job,started_at,status) VALUES (?,?,'queued')", (JOB, core.now_iso()))
        job = latest(db)
        core.audit(actor, "track17_full_pull", str(job["id"]), connection=db)
        return job


def _add_numbers(db, run_id, numbers):
    db.execute("""
        CREATE TABLE IF NOT EXISTS track17_full_queue (
            id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, number TEXT NOT NULL
        )
    """)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_track17_full_number ON track17_full_queue(run_id,number)")
    return db.executemany("INSERT OR IGNORE INTO track17_full_queue(run_id,number) VALUES (?,?)",
                          ((run_id, number) for number in numbers)).rowcount


def enqueue_import(db, numbers):
    """Queue a successful import or extend the full pull already underway."""
    if not numbers:
        return None
    job = latest(db)
    import_detail = {"lastImportAt": core.now_iso(), "lastImportWaybills": len(numbers)}
    if job and (job["status"] in ACTIVE or job["status"] == "failed" and "total" in job["detail"]):
        run_id = job["id"]
        detail = {**job["detail"], **import_detail}
        added = _add_numbers(db, run_id, numbers)
        if "total" in detail:
            detail["total"] += added
        detail.pop("reason", None)
        save(db, run_id, "running" if job["status"] == "running" else "queued", detail)
    else:
        detail = {"trigger": "import", **import_detail}
        run_id = db.execute("""
            INSERT INTO job_runs(job,started_at,status,detail) VALUES (?,?,'queued',?)
        """, (JOB, import_detail["lastImportAt"], json.dumps(detail))).lastrowid
        _add_numbers(db, run_id, numbers)
    core.audit("system", "track17_full_pull_import", str(run_id), import_detail, connection=db)
    return run_id


def seed(run_id, numbers):
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        job = latest(db)
        if not job or job["id"] != run_id or job["status"] not in ACTIVE or "total" in job["detail"]:
            raise RuntimeError("全量拉取任务已变更")
        _add_numbers(db, run_id, numbers)
        db.execute("DELETE FROM track17_full_queue WHERE run_id<>?", (run_id,))
        total = db.execute("SELECT count(*) FROM track17_full_queue WHERE run_id=?", (run_id,)).fetchone()[0]
        detail = {**job["detail"], "total": total, **dict.fromkeys(COUNTERS, 0)}
        save(db, run_id, "running" if total else "ok", detail)
        return total


def save(db, run_id, status, detail):
    db.execute("UPDATE job_runs SET status=?,finished_at=?,detail=? WHERE id=? AND job=?",
               (status, None if status in ACTIVE else core.now_iso(), json.dumps(detail), run_id, JOB))


def complete_batch(db, run_id, numbers, counts):
    job = latest(db)
    if not job or job["id"] != run_id or job["status"] not in ACTIVE:
        raise RuntimeError("全量拉取任务已变更")
    detail = job["detail"]
    db.executemany("DELETE FROM track17_full_queue WHERE run_id=? AND number=?",
                   ((run_id, number) for number in numbers))
    for key in COUNTERS:
        detail[key] += len(numbers) if key == "processed" else counts.get(key, 0)
    status = "ok" if detail["processed"] == detail["total"] else "running"
    save(db, run_id, status, detail)


def next_numbers(db, run_id, limit):
    return [row[0] for row in db.execute("""
        SELECT number FROM track17_full_queue WHERE run_id=? ORDER BY id LIMIT ?
    """, (run_id, limit))]


def get_job():
    with core.ops() as db:
        return latest(db)


def fail(run_id, reason):
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        job = latest(db)
        if job and job["id"] == run_id and job["status"] in ACTIVE:
            save(db, run_id, "failed", {**job["detail"], "reason": reason})


async def dispatch():
    """Resume persisted work on service startup, one batch at a time."""
    if os.name != "nt":
        return
    shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    while True:
        try:
            job = await asyncio.to_thread(get_job)
            if job and job["status"] in ACTIVE:
                runner = core.ROOT / "pull_all_tracking.ps1"
                try:
                    process = subprocess.Popen(
                        [str(shell), "-NoProfile", "-NonInteractive", "-File", str(runner), "-PythonPath",
                         sys.executable, "-RunId", str(job["id"])],
                        cwd=core.ROOT, creationflags=subprocess.CREATE_NO_WINDOW,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    while (result := process.poll()) is None:
                        await asyncio.sleep(2)
                    if result not in (0, 4):
                        await asyncio.to_thread(fail, job["id"], "credentials" if result == 5 else "batch")
                except OSError:
                    log.exception("Unable to start the 17TRACK full-pull runner")
                    await asyncio.to_thread(fail, job["id"], "launch")
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            log.exception("Unable to check the 17TRACK full-pull queue")
        await asyncio.sleep(INTERVAL)
