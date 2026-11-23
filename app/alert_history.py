"""Persist ended alert occurrences independently of the latest rules calculation."""

from .rules import visible_alerts

PENDING_STATES = "('open','claimed','handling')"
FINAL_STATES = "('resolved','archived')"
HANDLER = """coalesce(nullif(s.handler_name,''), CASE WHEN s.updated_by='open-test'
                THEN '开放测试（未记录姓名）' ELSE s.updated_by END)"""
HISTORY_METADATA = "state, handled_by, handle_at, handle_note, cleared_at"
ALERT_FIELDS = (
    "order_no", "rule", "rule_cn", "category", "level", "is_main", "evidence", "days",
    "order_status", "carrier", "warehouse", "country", "tracking_no", "track_status", "track_sub",
    "created_date", "created_at", "shipped_date", "shipped_at", "current_channel", "platform_code",
    "customer_order_no", "platform_order_no", "last_mile", "base_date", "synced_at", "first_seen_at",
)
HISTORY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS alert_history AS
       SELECT a.*, CAST(NULL AS TEXT) AS state, CAST(NULL AS TEXT) AS handled_by,
              CAST(NULL AS TEXT) AS handle_at, CAST(NULL AS TEXT) AS handle_note,
              CAST(NULL AS TEXT) AS cleared_at FROM alerts a WHERE 0""",
    "CREATE INDEX IF NOT EXISTS ix_alert_history_order ON alert_history(order_no)",
    "CREATE INDEX IF NOT EXISTS ix_alert_history_state ON alert_history(state, handle_at)",
)


def ensure_schema(db):
    for table, name in (("alerts", "first_seen_at"), ("alert_state", "handler_name")):
        if name not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
    for statement in HISTORY_SCHEMA:
        db.execute(statement)
    history_fields = {row[1] for row in db.execute("PRAGMA table_info(alert_history)")}
    for field in ALERT_FIELDS:
        if field not in history_fields:
            db.execute(f"ALTER TABLE alert_history ADD COLUMN {field} TEXT")


def alert_fields(alias):
    return ", ".join(f"{alias}.{field}" for field in ALERT_FIELDS)


def current_rows():
    return f"""SELECT {alert_fields('a')}, coalesce(s.status,'open') AS state, {HANDLER} AS handled_by,
                      s.updated_at AS handle_at, s.note AS handle_note, NULL AS cleared_at,
                      1 AS is_current, NULL AS history_id
               FROM alerts a LEFT JOIN alert_state s USING (order_no, rule)"""


def all_rows():
    return f"""{current_rows()} UNION ALL
               SELECT {alert_fields('h')}, {HISTORY_METADATA}, 0, h.rowid FROM alert_history h"""


def resolved_for_orders(db, order_numbers):
    if not order_numbers:
        return {}
    slots = ",".join("?" for _ in order_numbers)
    rows = db.execute(f"""
        WITH ranked AS (
            SELECT order_no, rule_cn, rule, state, handled_by, count(*) OVER by_order AS total,
                   row_number() OVER (by_order ORDER BY coalesce(handle_at,cleared_at,synced_at)
                       DESC NULLS LAST, rule) AS choice
            FROM ({all_rows()})
            WHERE order_no IN ({slots}) AND state IN ('resolved','recovered') AND {visible_alerts()}
            WINDOW by_order AS (PARTITION BY order_no)
        )
        SELECT order_no,rule_cn,rule,state,handled_by,total FROM ranked WHERE choice=1
    """, order_numbers)
    return {row["order_no"]: dict(row) for row in rows}


def carry_history(db, now):
    """Copy rules that cleared and preserve the first observation of continuing rules."""
    db.execute(f"""
        INSERT INTO alert_history ({', '.join(ALERT_FIELDS)}, {HISTORY_METADATA})
        SELECT {alert_fields('a')}, CASE WHEN s.status IN {FINAL_STATES} THEN s.status ELSE 'recovered' END,
               CASE WHEN s.status IN {FINAL_STATES} THEN {HANDLER} END,
               CASE WHEN s.status IN {FINAL_STATES} THEN s.updated_at END, s.note, ?
        FROM alerts a LEFT JOIN alert_state s USING (order_no, rule)
        WHERE NOT EXISTS (SELECT 1 FROM next_alerts n WHERE n.order_no=a.order_no AND n.rule=a.rule)
    """, [now])
    db.execute("""
        DELETE FROM alert_state AS s WHERE EXISTS (
            SELECT 1 FROM alerts a WHERE a.order_no=s.order_no AND a.rule=s.rule
        ) AND NOT EXISTS (
            SELECT 1 FROM next_alerts n WHERE n.order_no=s.order_no AND n.rule=s.rule
        )
    """)
    db.execute("""
        UPDATE next_alerts AS n SET first_seen_at=coalesce(
            (SELECT coalesce(a.first_seen_at,a.synced_at) FROM alerts a
             WHERE a.order_no=n.order_no AND a.rule=n.rule), n.synced_at)
    """)
