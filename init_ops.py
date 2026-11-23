"""
初始化操作层数据库（SQLite）。

分工：
  DuckDB 履约单.duckdb —— 事实层，管道重建，API 只读
  SQLite ops.db        —— 操作层，API 独占写入，记录"人对这批货做了什么"

DuckDB 保存当前命中的规则。SQLite 保存当前处理状态和结束的预警记录。
"""
import argparse
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.alert_history import HISTORY_SCHEMA

ROOT = Path(__file__).resolve().parent
OPS_DB = ROOT / "ops.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role         TEXT NOT NULL DEFAULT 'viewer',   -- admin / logistics / cs / viewer
    team         TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    sid        TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip         TEXT,
    ua         TEXT
);

-- 预警处理状态：键是 (履约单号, 规则)
CREATE TABLE IF NOT EXISTS alert_state (
    order_no   TEXT NOT NULL,
    rule       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',   -- open/claimed/handling/resolved/archived
    assignee   TEXT,
    note       TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    handler_name TEXT,
    PRIMARY KEY (order_no, rule)
);
CREATE INDEX IF NOT EXISTS ix_alert_state_status ON alert_state(status);
CREATE INDEX IF NOT EXISTS ix_alert_state_assignee ON alert_state(assignee);

-- 操作日志：谁在什么时候对哪张单做了什么
CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    actor  TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts DESC);

-- 后台任务运行记录
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,            -- running / ok / failed
    detail      TEXT
);


-- 预警镜像：由管道从 DuckDB 的 fct_alert 同步过来，供 API 直接分页筛选
CREATE TABLE IF NOT EXISTS alerts (
    order_no    TEXT NOT NULL,
    rule        TEXT NOT NULL,
    rule_cn     TEXT,
    category    TEXT,
    level       TEXT,
    is_main     INTEGER NOT NULL DEFAULT 1,
    evidence    TEXT,
    days        REAL,
    order_status TEXT,
    carrier     TEXT,
    warehouse   TEXT,
    country     TEXT,
    tracking_no TEXT,
    track_status TEXT,
    track_sub   TEXT,
    created_date TEXT,
    created_at   TEXT,
    shipped_date TEXT,
    shipped_at   TEXT,
    paid_at      TEXT,
    current_channel TEXT,
    platform_code TEXT,
    customer_order_no TEXT,
    platform_order_no TEXT,
    last_mile TEXT,
    base_date   TEXT,
    synced_at   TEXT,
    first_seen_at TEXT,
    PRIMARY KEY (order_no, rule)
);
CREATE INDEX IF NOT EXISTS ix_alerts_rule ON alerts(rule);
CREATE INDEX IF NOT EXISTS ix_alerts_level ON alerts(level);
CREATE INDEX IF NOT EXISTS ix_alerts_wh ON alerts(warehouse);
CREATE INDEX IF NOT EXISTS ix_alerts_cr ON alerts(carrier);
CREATE INDEX IF NOT EXISTS ix_alerts_days ON alerts(days DESC);
CREATE INDEX IF NOT EXISTS ix_alerts_order ON alerts(order_no);

-- 订单备注（独立于预警，随手记）
CREATE TABLE IF NOT EXISTS order_notes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    ts       TEXT NOT NULL,
    actor    TEXT,
    body     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_order_notes ON order_notes(order_no, ts DESC);
""" + ";\n".join(HISTORY_SCHEMA) + ";\n"

SEED_USERS = [
    # username, password, display, role, team
    ("admin", "admin123", "系统管理员", "admin", None),
    ("wuliu", "wuliu123", "物流专员", "logistics", "LM"),
    ("kefu", "kefu123", "客服专员", "cs", None),
]


def hash_pw(password: str, salt: bytes | None = None) -> str:
    """scrypt 口令散列，格式 scrypt$<salt_hex>$<hash_hex>"""
    import hashlib
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_pw(password: str, stored: str) -> bool:
    import hashlib
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def init(db: Path = OPS_DB, seed: bool = True):
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    if seed:
        for username, pw, display, role, team in SEED_USERS:
            row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if row:
                continue
            con.execute(
                "INSERT INTO users (username,password_hash,display_name,role,team,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (username, hash_pw(pw), display, role, team,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            print(f"  建用户 {username} / {pw}  ({role})")
        con.commit()
    n = con.execute("SELECT count(*) FROM users").fetchone()[0]
    con.close()
    print(f"操作层就绪: {db}  用户 {n} 个")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(OPS_DB))
    ap.add_argument("--no-seed", action="store_true")
    a = ap.parse_args()
    init(Path(a.db), seed=not a.no_seed)
