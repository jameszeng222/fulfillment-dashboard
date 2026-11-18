"""系统核心：配置、双库连接、会话与权限。"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parent.parent
DUCK_DB = ROOT / "履约单.duckdb"
OPS_DB = ROOT / "ops.db"

SESSION_COOKIE = "fd_session"
SESSION_TTL_HOURS = 12

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


def create_session(username: str, ip: str = "", ua: str = "") -> tuple[str, str]:
    sid = new_sid()
    exp = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).isoformat(timespec="seconds")
    with ops() as con:
        con.execute("INSERT INTO sessions (sid,username,created_at,expires_at,ip,ua) VALUES (?,?,?,?,?,?)",
                    (sid, username, now_iso(), exp, ip, (ua or "")[:200]))
        # 顺手清理过期会话
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
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
            SELECT s.username, s.expires_at, u.display_name, u.role, u.team, u.active
            FROM sessions s JOIN users u ON u.username = s.username
            WHERE s.sid = ?
        """, (sid,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < now_iso() or not row["active"]:
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


def audit(actor: str, action: str, target: str = "", detail=None):
    with ops() as con:
        con.execute("INSERT INTO audit_log (ts,actor,action,target,detail) VALUES (?,?,?,?,?)",
                    (now_iso(), actor, action, target,
                     json.dumps(detail, ensure_ascii=False) if detail is not None else None))


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
