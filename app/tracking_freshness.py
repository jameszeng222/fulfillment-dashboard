"""Scope and audit records for regular 17TRACK verification."""
from contextlib import closing

from . import core, tracking_archive


TABLE = "trk_verification"
OLDEST_DISPATCH = "date_trunc('day', current_timestamp AT TIME ZONE 'Asia/Shanghai') - INTERVAL '5 months'"


def scope(con):
    status, _, archive = tracking_archive.sources(con)
    checks = TABLE if core.columns(con, TABLE) else """(
        SELECT NULL::VARCHAR AS number, NULL::TIMESTAMP AS checked_at,
        NULL::TIMESTAMP AS verified_at, NULL::VARCHAR AS outcome WHERE FALSE
    )"""
    return f"""
        WITH clock AS (SELECT current_timestamp AT TIME ZONE 'Asia/Shanghai' AS now),
        scope AS (
            SELECT o.履约单号 AS order_no, o.单号归一 AS number,
                   o.物流商, o.C端承运商, o.签出时间 AS shipped_at,
                   t.carrier, t.最新事件时间 AS last_event, t.状态 AS last_status,
                   c.checked_at, c.verified_at, c.outcome, now,
                   o.签出时间 < date_trunc('day', now) - INTERVAL '3 months' AS older
            FROM fct_order o
            CROSS JOIN clock
            LEFT JOIN {status} t ON t.单号=o.单号归一
            LEFT JOIN {archive} h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
            LEFT JOIN {checks} c ON c.number=o.单号归一
            WHERE {core.active_order()} AND length(o.单号归一) BETWEEN 8 AND 50
              AND lower(coalesce(trim(o.物流商),''))<>'postnet'
              AND lower(coalesce(trim(o.C端承运商),''))<>'postnet'
              AND o.签出时间 BETWEEN {OLDEST_DISPATCH} AND now
              AND (o.签出时间>=date_trunc('day', now)-INTERVAL '3 months' OR o.已签收=0)
              AND (coalesce(t.状态,'')<>'Delivered' OR (
                   NOT (coalesce(h.事件数,0)>0 AND h.carrier IS NOT DISTINCT FROM t.carrier)
                   AND NOT EXISTS (
                       SELECT 1 FROM trk_event e WHERE e.单号=o.单号归一
                         AND (e.运输商代码=t.carrier OR e.运输商代码 IS NULL))))
            QUALIFY row_number() OVER (PARTITION BY o.履约单号
                       ORDER BY t.最新事件时间 DESC NULLS LAST, t.carrier)=1
        )
    """


def candidates(con, limit):
    return con.execute(scope(con) + """
        SELECT number, 物流商, C端承运商, carrier, last_event, last_status,
               count(DISTINCT nullif(lower(trim(C端承运商)),'')) OVER (PARTITION BY number)>1 AS conflict
        FROM scope
        WHERE checked_at IS NULL OR checked_at <= now - CASE WHEN outcome='read_error'
              THEN INTERVAL '6 hours' ELSE INTERVAL '12 hours' END
        QUALIFY row_number() OVER (PARTITION BY number ORDER BY shipped_at DESC, order_no)=1
        ORDER BY checked_at NULLS FIRST, shipped_at DESC, number LIMIT ?
    """, [limit]).fetchall()


def save_checks(con, rows):
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            number VARCHAR PRIMARY KEY, carrier INTEGER, checked_at TIMESTAMP NOT NULL,
            verified_at TIMESTAMP, outcome VARCHAR NOT NULL, error_code INTEGER
        )
    """)
    con.executemany(f"""
        INSERT INTO {TABLE} VALUES (?,?,?,?,?,?)
        ON CONFLICT (number) DO UPDATE SET carrier=excluded.carrier, checked_at=excluded.checked_at,
            verified_at=CASE WHEN excluded.carrier IS DISTINCT FROM {TABLE}.carrier THEN excluded.verified_at
                             ELSE coalesce(excluded.verified_at,{TABLE}.verified_at) END,
            outcome=excluded.outcome, error_code=excluded.error_code
    """, rows)


def order_check(con, number):
    if not number or not core.columns(con, TABLE):
        return None
    result = con.execute(f"""
        SELECT checked_at AS lastAttempt, verified_at AS lastVerified, outcome, error_code AS errorCode
        FROM {TABLE} WHERE number=?
    """, [number])
    row = result.fetchone()
    return dict(zip((column[0] for column in result.description), row)) if row else None


def coverage(con=None):
    if con is None:
        with closing(core.duck()) as reader:
            return coverage(reader)
    result = con.execute(scope(con) + """
        SELECT count(*) AS orders, count(DISTINCT number) AS waybills,
               count(*) FILTER (WHERE older) AS olderOrders,
               count(*) FILTER (WHERE checked_at IS NULL) AS uncheckedOrders,
               count(*) FILTER (WHERE verified_at>=now-INTERVAL '24 hours') AS verified24h,
               count(*) FILTER (WHERE older AND checked_at IS NULL) AS olderUnchecked,
               count(*) FILTER (WHERE older AND verified_at>=now-INTERVAL '24 hours') AS olderVerified24h,
               count(*) FILTER (WHERE outcome IS NOT NULL AND outcome<>'verified') AS exceptionOrders
        FROM scope
    """)
    return dict(zip((column[0] for column in result.description), result.fetchone()))
