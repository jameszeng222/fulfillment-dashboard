"""Locate the earliest verifiable carrier pickup for each scoped fulfillment."""
from . import core
from .tracking_archive import EVENT_TABLE, TABLE


def _pickup_test(stage, sub, text):
    desc = f"lower(trim(coalesce({text},'')))"
    return f"""(
        {stage}='PickedUp' OR {sub}='InTransit_PickedUp'
        OR regexp_matches({desc}, '^(shipment pickup|shipment picked up|picked up|pickup scan|已揽收|承运商已提取)( |$)')
    ) AND NOT regexp_matches({desc},
      'awaiting|label created|information received|information sent|scheduled for pickup|available for pickup|not yet picked up')"""


def build_pickups(con, orders="regional_scope"):
    """Choose the matching query carrier, then its first physical pickup in UTC.

    A pickup before ERP dispatch remains the first pickup. The elapsed-time
    calculation excludes that inconsistent pair rather than choosing a later scan.
    """
    sources = []
    fields = core.columns(con, "trk_event")
    if fields:
        timestamp = "e.事件时间"
        if {"当地时间", "运输商代码"} <= fields:
            timestamp = """CASE WHEN e.运输商代码=191376 AND
                       (contains(lower(coalesce(e.地点,'')), 'china') OR contains(coalesce(e.地点,''),'中国'))
                       THEN coalesce(try_cast(substr(e.当地时间,1,19) AS TIMESTAMP)-INTERVAL '8 hours',e.事件时间)
                       ELSE e.事件时间 END"""
        carrier = "e.运输商代码" if "运输商代码" in fields else "NULL"
        sources.append(f"""
            SELECT e.单号 AS number,{carrier} AS carrier,{timestamp} AS picked
            FROM trk_event e SEMI JOIN numbers n ON n.number=e.单号
            WHERE e.事件时间 IS NOT NULL AND {_pickup_test('e.主状态','e.子状态','e.描述')}
        """)
    if core.columns(con, EVENT_TABLE):
        sources.append(f"""
            SELECT e.tracking_no AS number,e.carrier_code AS carrier,
                   CASE WHEN e.carrier_code=191376 AND
                       (contains(lower(coalesce(e.location,'')), 'china') OR contains(coalesce(e.location,''),'中国'))
                       THEN coalesce(try_cast(
                           json_extract_string(try_cast(e.event_time_raw AS JSON),'$.date') || ' ' ||
                           json_extract_string(try_cast(e.event_time_raw AS JSON),'$.time') AS TIMESTAMP)-INTERVAL '8 hours',
                           timezone('UTC',try_cast(e.event_time_utc AS TIMESTAMPTZ)))
                       ELSE timezone('UTC',try_cast(e.event_time_utc AS TIMESTAMPTZ)) END AS picked
            FROM {EVENT_TABLE} e SEMI JOIN numbers n ON n.number=e.tracking_no
            WHERE e.event_time_utc IS NOT NULL AND {_pickup_test('e.stage','e.substatus','e.description')}
        """)
    if core.columns(con, "trk_milestone"):
        sources.append("""
            SELECT m.单号 AS number,s.carrier,m.时间 AS picked
            FROM trk_milestone m SEMI JOIN numbers n ON n.number=m.单号
            JOIN trk_status s ON s.单号=m.单号
            WHERE m.节点='PickedUp' AND m.时间 IS NOT NULL
        """)
    archive_join = ""
    expected = "s.carrier"
    if core.columns(con, TABLE):
        archive_join = f"""
            LEFT JOIN (
                SELECT 履约单号,单号,min(carrier) AS carrier FROM {TABLE}
                WHERE 回补状态='tracked' GROUP BY 1,2 HAVING count(DISTINCT carrier)=1
            ) h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
        """
        expected = "coalesce(h.carrier,s.carrier)"
    union = " UNION ALL ".join(sources) or "SELECT NULL::VARCHAR AS number,NULL::INT AS carrier,NULL::TIMESTAMP AS picked WHERE FALSE"
    con.execute(f"""
        CREATE TEMP TABLE regional_pickups AS
        WITH numbers AS (SELECT DISTINCT 单号归一 AS number FROM {orders} WHERE 单号归一 IS NOT NULL),
        scans AS ({union}),
        first_scans AS (
            SELECT number,carrier,min(picked) AS picked FROM scans WHERE picked IS NOT NULL GROUP BY 1,2
        )
        SELECT o.履约单号, min(p.picked)+INTERVAL '8 hours' AS pickup
        FROM {orders} o LEFT JOIN trk_status s ON s.单号=o.单号归一 {archive_join}
        JOIN first_scans p ON p.number=o.单号归一 AND p.carrier={expected}
        GROUP BY o.履约单号
    """)
