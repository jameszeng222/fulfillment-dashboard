"""系统核心：配置、双库连接、会话与权限。"""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import duckdb
from fastapi import HTTPException, Request
from .ops_gateway import setting, site_origin

ROOT = Path(__file__).resolve().parent.parent
DUCK_DB = ROOT / "履约单.duckdb"
OPS_DB = ROOT / "ops.db"

SESSION_COOKIE = "fd_session"
SESSION_TTL_HOURS = 12
CLOUD_VIEWER_MARKER = "!cloud-viewer-invite"

ROLES = {
    "admin":     {"label": "系统管理员", "can_edit": True, "scope": "all"},
    "logistics": {"label": "物流专员",   "can_edit": True, "scope": "logistics"},
    "cs":        {"label": "客服专员",   "can_edit": True, "scope": "cs"},
    "viewer":    {"label": "只读",       "can_edit": False, "scope": "all"},
}


# ---------------------------------------------------------------- 连接

def duck() -> duckdb.DuckDBPyConnection:
    """事实层只读连接。每次请求新建，用完即关（DuckDB 连接不是线程安全的）。"""
    return duckdb.connect(str(DUCK_DB), read_only=True)


def snapshot_key():
    """Invalidate read caches when the updater publishes a different facts file."""
    filename = DUCK_DB.resolve()
    info = filename.stat()
    try:
        wal = filename.with_name(filename.name + ".wal").stat()
        journal = (wal.st_size, wal.st_mtime_ns)
    except FileNotFoundError:
        journal = ()
    return filename, info.st_ino, info.st_size, info.st_mtime_ns, journal


def columns(con, table):
    return {row[0] for row in con.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = ?""", [table],
    ).fetchall()}


def order_facts(con):
    if "当前渠道" in columns(con, "fct_order"):
        return "fct_order"
    if "当前渠道" not in columns(con, "履约单明细"):
        return "(SELECT *, NULL::VARCHAR AS 当前渠道 FROM fct_order)"
    return """(SELECT f.*, c.当前渠道 FROM fct_order f LEFT JOIN (
        SELECT 履约单号, max(nullif(trim(当前渠道),'')) AS 当前渠道
        FROM 履约单明细 GROUP BY 履约单号
    ) c USING (履约单号))"""


def active_order(alias="o"):
    return f"{alias}.状态 IS DISTINCT FROM '已取消'"


def active_facts(facts="fct_order"):
    return f"(SELECT * FROM {facts} o WHERE {active_order()})"


def filter_values(value):
    if not value:
        return []
    if value.startswith("["):
        try:
            items = json.loads(value)
        except ValueError:
            items = None
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            return [item for item in items if item]
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def _read_platform_names(path, modified_at):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        return {str(row[0]).strip(): str(row[1]).strip()
                for row in book.active.iter_rows(min_row=2, max_col=2, values_only=True)
                if row[0] and row[1]}
    finally:
        book.close()


def platform_names():
    path = ROOT / "reference" / "平台对照表.xlsx"
    return _read_platform_names(path, path.stat().st_mtime_ns) if path.exists() else {}


def require_admin(request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以配置规则")
    return user


@contextmanager
def ops():
    """操作层连接。WAL 模式，短事务。"""
    con = sqlite3.connect(OPS_DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------- 会话

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_sid() -> str:
    return secrets.token_urlsafe(32)


def session_seconds():
    try:
        seconds = int(setting("SESSION_TTL_SEC") or SESSION_TTL_HOURS * 3600)
        return seconds if 60 <= seconds <= 86400 else SESSION_TTL_HOURS * 3600
    except ValueError:
        return SESSION_TTL_HOURS * 3600


def secure_cookie(request):
    return bool(site_origin()) or request.url.scheme == "https"


def set_session_cookie(request, response, sid, max_age=None):
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, secure=secure_cookie(request),
                        samesite="lax", max_age=max_age or session_seconds(), path="/")


def csrf_token(sid):
    return hashlib.sha256(("fd-csrf:" + sid).encode()).hexdigest()


def allowed_online_account(username):
    allowed = {name.strip() for name in setting("FULFILLMENT_ONLINE_USERS").split(",") if name.strip()}
    return not allowed or username in allowed


def allowed_session_account(user):
    return allowed_online_account(user["username"]) or (
        setting("FULFILLMENT_CLOUD_TICKETS").lower() == "true"
        and user["username"].startswith("cloud-viewer:")
        and user["role"] == "viewer"
        and user["password_hash"] == CLOUD_VIEWER_MARKER
    )


def create_session(username: str, ip: str = "", ua: str = "", max_age=None) -> tuple[str, str]:
    sid = new_sid()
    exp = (datetime.now(timezone.utc) + timedelta(seconds=max_age or session_seconds())).isoformat(timespec="seconds")
    with ops() as con:
        con.execute("INSERT INTO sessions (sid,username,created_at,expires_at,ip,ua) VALUES (?,?,?,?,?,?)",
                    (sid, username, now_iso(), exp, ip, (ua or "")[:200]))
        # 顺手清理过期会话
        con.execute("DELETE FROM sessions WHERE julianday(expires_at)<=julianday('now')")
    return sid, exp


def drop_session(sid: str):
    if not sid:
        return
    with ops() as con:
        con.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def current_user(request: Request) -> dict | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    with ops() as con:
        row = con.execute("""
            SELECT s.username, s.expires_at, u.display_name, u.role, u.team, u.active, u.password_hash
            FROM sessions s JOIN users u ON u.username = s.username
            WHERE s.sid = ?
        """, (sid,)).fetchone()
    if not row:
        return None
    if (datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)
            or not row["active"] or not allowed_session_account(row)):
        return None
    return {
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "team": row["team"],
        "can_edit": ROLES.get(row["role"], {}).get("can_edit", False),
        "role_label": ROLES.get(row["role"], {}).get("label", row["role"]),
    }


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return u


def require_editor(request: Request) -> dict:
    u = require_user(request)
    if not u["can_edit"]:
        raise HTTPException(status_code=403, detail="当前角色没有操作权限")
    return u


def audit(actor: str, action: str, target: str = "", detail=None, *, connection=None):
    sql = "INSERT INTO audit_log (ts,actor,action,target,detail) VALUES (?,?,?,?,?)"
    record = (now_iso(), actor, action, target,
              json.dumps(detail, ensure_ascii=False) if detail is not None else None)
    if connection is not None:
        connection.execute(sql, record)
        return
    with ops() as con:
        con.execute(sql, record)


# ---------------------------------------------------------------- 工具

def j(obj) -> str:
    """统一 JSON 输出：pandas/numpy 类型转原生。"""
    import math
    import numpy as np

    def conv(o):
        if isinstance(o, dict):
            return {k: conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            f = float(o)
            return None if math.isnan(f) or math.isinf(f) else f
        if isinstance(o, float):
            return None if math.isnan(o) or math.isinf(o) else o
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return o

    return json.dumps(conv(obj), ensure_ascii=False, default=str)

def clean(o):
    """把 pandas/numpy 的 NaN/NaT/np 类型转成 JSON 安全的值，供 FastAPI 直接返回。"""
    import math

    import numpy as np

    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if o is None:
        return None
    if hasattr(o, 'isoformat'):
        return o.isoformat()
    try:
        import pandas as pd
        if o is pd.NaT:
            return None
    except Exception:
        pass
    return o
