"""Operational status and the durable inbox for 17TRACK pushes."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import core, ops_auth, tracking_freshness, tracking_full
from .waybill import normalized


POLL_JOB = "17TRACK 滚动更新"
PUSH_JOB = "17TRACK Webhook 入库"
POLL_TASK = "履约控制塔-17TRACK滚动更新"
WEBHOOK_PATH = "/api/17track/webhook"
MAX_PUSH_BYTES = 1024 * 1024
router = APIRouter()


def init_inbox(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS track17_inbox (
            id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
            event TEXT NOT NULL, number TEXT, carrier INTEGER, payload TEXT,
            received_at TEXT NOT NULL, next_attempt_at TEXT,
            state TEXT NOT NULL, reason TEXT, processed_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS ix_track17_inbox_queue ON track17_inbox(state,next_attempt_at,id)")


def begin_poll():
    with core.ops() as db:
        now = core.now_iso()
        db.execute("""
            UPDATE job_runs SET status='failed',finished_at=?,detail=?
            WHERE job=? AND status='running'
        """, (now, json.dumps({"reason": "interrupted"}), POLL_JOB))
        return db.execute(
            "INSERT INTO job_runs(job,started_at,status) VALUES (?,?,'running')",
            (POLL_JOB, now),
        ).lastrowid


def finish_poll(db, run_id, status, detail=None):
    db.execute("""
        UPDATE job_runs SET finished_at=?,status=?,detail=? WHERE id=? AND job=? AND status='running'
    """, (core.now_iso(), status, json.dumps(detail), run_id, POLL_JOB))


def public_url():
    try:
        value = os.environ.get("TRACK17_WEBHOOK_URL", "").strip()
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme == "https" and "." in host and host not in {"127.0.0.1", "0.0.0.0"}
                and not (url.username or url.password or url.query or url.fragment)):
            return value
    except ValueError:
        pass
    return None


def webhook_key():
    key = (os.environ.get("TRACK17_WEBHOOK_KEY") or os.environ.get("TRACK17_TOKEN") or "").strip()
    if key or os.name != "nt":
        return key
    path = Path(os.environ.get("LOCALAPPDATA", "")) / "FulfillmentControlTower" / "track17.dpapi"
    try:
        return _decrypt_dpapi(bytes.fromhex(path.read_text(encoding="ascii").strip()))
    except (OSError, ValueError):
        return ""


def _decrypt_dpapi(encrypted):
    from ctypes import POINTER, Structure, c_ubyte, byref, cast, string_at, windll
    from ctypes.wintypes import DWORD, HLOCAL

    class Blob(Structure):
        _fields_ = [("size", DWORD), ("data", POINTER(c_ubyte))]

    buffer = (c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source = Blob(len(encrypted), buffer)
    output = Blob()
    if not windll.crypt32.CryptUnprotectData(byref(source), None, None, None, None, 1, byref(output)):
        return ""
    try:
        return string_at(output.data, output.size).decode("utf-16-le").strip()
    finally:
        windll.kernel32.LocalFree(cast(output.data, HLOCAL))


def _payload_for_storage(number, carrier, info):
    # Persist the inputs used by the tracking writer. Recipient and shipper fields are
    # unnecessary here, including addresses nested inside the provider events.
    def fields(value, names):
        return {name: value[name] for name in names if isinstance(value, dict) and name in value}

    event_fields = ("time_utc", "time_iso", "time_raw", "stage", "sub_status", "description", "location")
    tracking = info.get("tracking") or {}
    providers = [
        {"provider": fields(group.get("provider"), ("key", "name", "alias")),
         "events": [fields(event, event_fields) for event in group.get("events") or []]}
        for group in tracking.get("providers") or []
    ]
    return {"number": number, "carrier": carrier, "track_info": {
        "latest_status": fields(info.get("latest_status"), ("status", "sub_status")),
        "latest_event": fields(info.get("latest_event"), event_fields),
        "time_metrics": info.get("time_metrics"),
        "misc_info": fields(info.get("misc_info"), ("risk_factor",)),
        "milestone": [fields(event, (*event_fields, "key_stage")) for event in info.get("milestone") or []],
        "tracking": {"providers": providers},
    }}


def enqueue(body, signature):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", signature or ""):
        raise HTTPException(status_code=401, detail="签名校验失败")
    key = webhook_key()
    if not key:
        raise HTTPException(status_code=503, detail="接收服务尚未配置签名密钥")
    expected = hashlib.sha256(body + b"/" + key.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise HTTPException(status_code=401, detail="签名校验失败")
    try:
        message = json.loads(body.decode("utf-8"))
        event = message["event"]
        if not isinstance(event, str) or not re.fullmatch(r"[A-Z_]{1,50}", event):
            raise ValueError
        data = message.get("data") or {}
        number = normalized(data.get("number"))
        carrier = data.get("carrier")
        info = data.get("track_info")
        if event == "TRACKING_UPDATED":
            if not re.fullmatch(r"[0-9A-Z-]{5,50}", number or "") or type(carrier) is not int or carrier <= 0:
                raise ValueError
            if not isinstance(info, dict) or not info:
                raise ValueError
            payload = json.dumps(_payload_for_storage(number, carrier, info), ensure_ascii=False)
        else:
            number = number if isinstance(number, str) and len(number) <= 50 else None
            carrier = carrier if type(carrier) is int and 0 < carrier < 2**31 else None
            payload = None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(status_code=400, detail="推送格式无效") from None
    now = core.now_iso()
    with core.ops() as db:
        init_inbox(db)
        row = db.execute("""
            INSERT INTO track17_inbox(digest,event,number,carrier,payload,received_at,state,
                                      reason,processed_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING
        """, (hashlib.sha256(body).hexdigest(), event, number, carrier, payload, now,
              "queued" if payload else "ignored", None if payload else "notification_only",
              None if payload else now))
        return {"received": True, "duplicate": row.rowcount == 0}


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def receive_webhook(request: Request):
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > MAX_PUSH_BYTES:
            raise HTTPException(status_code=413, detail="推送体积超出限制")
        body.extend(part)
    return await run_in_threadpool(enqueue, bytes(body), request.headers.get("sign", ""))


def ready_pushes():
    with core.ops() as db:
        init_inbox(db)
        return db.execute("""
            SELECT id,number,carrier,payload,received_at FROM track17_inbox
            WHERE state='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY id LIMIT 1000
        """, (core.now_iso(),)).fetchall()


def finish_pushes(db, outcomes):
    now = core.now_iso()
    retry = (datetime.fromisoformat(now) + timedelta(minutes=30)).isoformat()
    for push_id, state, reason in outcomes:
        if state == "queued":
            db.execute("UPDATE track17_inbox SET reason=?,next_attempt_at=? WHERE id=?",
                       (reason, retry, push_id))
        else:
            db.execute("""
                UPDATE track17_inbox SET state=?,reason=?,processed_at=?,payload=NULL,next_attempt_at=NULL
                WHERE id=?
            """, (state, reason, now, push_id))


@lru_cache(maxsize=1)
def windows_schedule(_cache_bucket):
    if os.name != "nt":
        return None
    script = f"""
        $task = Get-ScheduledTask -TaskName '{POLL_TASK}' -ErrorAction Stop
        $info = $task | Get-ScheduledTaskInfo
        $next = if ($info.NextRunTime.Year -gt 2000) {{ $info.NextRunTime.ToString('o') }}
        [Console]::WriteLine((@{{state=$task.State.ToString(); next=$next;
            result=$info.LastTaskResult}} | ConvertTo-Json -Compress))
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(path), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, timeout=5, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@router.get("/api/17track/sync-status")
def sync_status(request: Request):
    core.require_user(request)
    with core.ops() as db:
        init_inbox(db)
        runs = [dict(row) for row in db.execute("""
            SELECT started_at,finished_at,status,detail FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 10
        """, (POLL_JOB,))]
        success = db.execute("""
            SELECT finished_at FROM job_runs WHERE job=? AND status='ok' ORDER BY id DESC LIMIT 1
        """, (POLL_JOB,)).fetchone()
        totals = dict(db.execute("""
            SELECT count(*) FILTER (WHERE state='queued') AS queued,
                   count(*) FILTER (WHERE state='applied') AS applied,
                   max(received_at) AS lastReceived,
                   max(processed_at) FILTER (WHERE state='applied') AS lastApplied
            FROM track17_inbox
        """).fetchone())
        pushes = [dict(row) for row in db.execute("""
            SELECT event,received_at,processed_at,state,reason FROM track17_inbox ORDER BY id DESC LIMIT 10
        """)]
        full_pull = tracking_full.latest(db)
    for run in runs:
        try:
            run["detail"] = json.loads(run["detail"] or "null")
        except ValueError:
            run["detail"] = None
    coverage, conflicts = None, []
    if core.DUCK_DB.is_file():
        with closing(core.duck()) as con:
            coverage, conflicts = tracking_freshness.coverage(con), tracking_freshness.carrier_conflicts(con)
    return {"now": core.now_iso(), "fullPull": full_pull, "fullPullAvailable": os.name == "nt",
            "poll": {"schedule": windows_schedule(int(time.monotonic() // 30)),
                                           "coverage": coverage, "carrierConflicts": conflicts,
                                           "lastSuccess": success[0] if success else None, "runs": runs},
            "webhook": {**totals, "url": public_url(), "ready": bool(webhook_key()), "pushes": pushes}}


@router.post("/api/17track/full-pull", status_code=202)
def start_full_pull(request: Request):
    ops_auth.require_csrf(request)
    user = core.require_editor(request)
    if os.name != "nt":
        raise HTTPException(503, "当前服务未连接本机拉取任务")
    return tracking_full.enqueue(user["username"])
