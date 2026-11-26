"""Calculate batch signals from the current order scope and stored 17TRACK events."""
from datetime import datetime, timedelta, timezone
from threading import Lock

from . import core


SCAN_HOURS = 48
STALE_HOURS = 72
STATION_HOURS = 72
BEIJING = timezone(timedelta(hours=8))
RUNNING = Lock()

SIGNALS = {
    "scan": "首条实物扫描偏晚",
    "stale": "同地点轨迹断更",
    "station": "到派送站整体变慢",
    "exception": "同类异常事件",
}


def _rows(con, sql, parameters=()):
    cursor = con.execute(sql, parameters)
    fields = [column[0] for column in cursor.description]
    return [dict(zip(fields, row)) for row in cursor.fetchall()]


def _events(con):
    sources = []
    fields = core.columns(con, "trk_event")
    if fields:
        carrier_match = "AND (e.运输商代码=s.carrier OR e.运输商代码 IS NULL)" if "运输商代码" in fields else ""
        sources.append(f"""
            SELECT s.id,e.事件时间 AS instant,e.主状态 AS stage,e.子状态 AS sub,e.描述 AS description,e.地点 AS place
            FROM trk_event e JOIN analysis_orders s ON e.单号=s.num {carrier_match}
        """)
    if core.columns(con, "trk_archive_event"):
        sources.append("""
            SELECT s.id,timezone('UTC',try_cast(e.event_time_utc AS TIMESTAMPTZ)),e.stage,e.substatus,e.description,e.location
            FROM trk_archive_event e JOIN analysis_orders s ON e.tracking_no=s.num AND e.carrier_code=s.carrier
        """)
    return " UNION ALL ".join(sources) or """
        SELECT NULL::VARCHAR AS id,NULL::TIMESTAMP AS instant,NULL::VARCHAR AS stage,
               NULL::VARCHAR AS sub,NULL::VARCHAR AS description,NULL::VARCHAR AS place WHERE FALSE
    """


def report(con):
    asof = datetime.now(BEIJING).replace(tzinfo=None, microsecond=0)
    con.execute(f"""
        CREATE TEMP TABLE analysis_events AS
        WITH raw AS ({_events(con)}), cleaned AS (
            SELECT id,instant + INTERVAL '8 hours' AS event_at,coalesce(stage,'') AS stage,
                   coalesce(sub,'') AS sub,lower(trim(coalesce(description,''))) AS description,
                   nullif(trim(place),'') AS place
            FROM raw
        )
        SELECT id,event_at,place,
          CASE
            WHEN sub LIKE 'Exception_Damage%' OR regexp_matches(description,'package (is )?damaged') THEN '破损'
            WHEN regexp_matches(sub,'Exception_Return|Returning|Returned')
                 OR regexp_matches(description,'return (to sender|initiated|in progress)|returned to sender') THEN '退运'
            WHEN regexp_matches(description,'incorrect/insufficient address|invalid address|incomplete address|address (is )?insufficient') THEN '地址异常'
            WHEN regexp_matches(description,'business closed|no access|refused by|restricted address') THEN '派送受阻'
            WHEN sub LIKE 'DeliveryFailure_%' OR stage='DeliveryFailure' THEN '投递失败'
            WHEN sub LIKE 'Exception_%' OR stage='Exception' THEN '承运商异常'
          END AS issue,
          CASE
            WHEN regexp_matches(description,'awaiting item|label created|information (sent|received|submitted)|pre.?shipment') THEN 'info'
            WHEN regexp_matches(description,'^arrived at .*delivery station|^arrived at (local|destination) .*facility') THEN 'station'
            WHEN regexp_matches(description,'^arrived at .*regional hub') THEN 'hub'
            WHEN stage='Delivered' OR sub LIKE 'Delivered_%' OR regexp_matches(description,'^delivered[, .]') THEN 'delivered'
            WHEN stage IN ('PickedUp','Departure','Arrival','OutForDelivery')
                OR regexp_matches(sub,'^InTransit_(PickedUp|Departure|Arrival|Other)$|^OutForDelivery_')
                OR regexp_matches(description,'^(arrived at|departed |loaded on|shipment pickup|out for delivery|in transit to)') THEN 'scan'
            ELSE 'other'
          END AS node
        FROM cleaned
        GROUP BY ALL
    """)
    con.execute("""
        CREATE TEMP TABLE analysis_steps AS
        WITH milestones AS (
            SELECT e.id, count(*) AS events, max(event_at) AS last_at,
              min(event_at) FILTER (WHERE node IN ('station','hub','scan','delivered') AND event_at>=shipped) AS first_scan,
              min(event_at) FILTER (WHERE node='station' AND event_at>=shipped) AS station_at,
              arg_min(place,event_at) FILTER (WHERE node='hub' AND event_at>=shipped) AS hub,
              arg_max_null(place,event_at) AS latest_event_place,
              arg_max(node,event_at) AS last_node,
              bool_or(node='delivered' AND event_at>=shipped) AS event_delivered,
              bool_or(issue IS NOT NULL) AS has_issue
            FROM analysis_events e JOIN analysis_orders s USING(id) GROUP BY 1
        )
        SELECT s.*,m.* EXCLUDE(id),m.last_at AS last_seen,
          m.latest_event_place AS last_place,
          coalesce(s.official='Delivered',false) OR coalesce(m.event_delivered,false) OR s.wms_delivered AS delivered,
          date_diff('second',shipped,first_scan)/3600.0 AS scan_h,
          CASE WHEN station_at>=first_scan THEN date_diff('second',first_scan,station_at)/3600.0 END AS station_h
        FROM analysis_orders s LEFT JOIN milestones m USING(id)
    """)
    summary = _rows(con, """
        SELECT count(*) AS total,count(events) AS tracked,
               count(*) FILTER (WHERE official='Delivered' OR event_delivered) AS confirmed,
               count(*) FILTER (WHERE has_issue) AS "explicit",
               max(checked) AS checked,min(shipped) AS first_ship,max(shipped) AS last_ship,
               count(*) FILTER (WHERE scan_h>?) AS "lateScans",
               count(*) FILTER (WHERE NOT delivered AND last_seen<=? - to_hours(?)) AS stale
        FROM analysis_steps
    """, [SCAN_HOURS, asof, STALE_HOURS])[0]
    first_scans = _rows(con, """
        WITH groups AS (
          SELECT date_trunc('hour',shipped) AS period,warehouse,carrier_name,channel,count(*) AS total,
            min(shipped) AS first_shipped,max(shipped) AS last_shipped,
            count(*) FILTER (WHERE late) AS affected,
            count(*) FILTER (WHERE late AND NOT delivered) AS open,
            count(*) FILTER (WHERE late AND first_scan IS NULL) AS missing,
            round(median(scan_h) FILTER (WHERE late),1) AS hours,
            mode(hub) FILTER (WHERE late) AS place,
            list(id ORDER BY delivered,scan_h DESC NULLS FIRST,id) FILTER (WHERE late) AS samples
          FROM (
            SELECT *, events IS NOT NULL AND
              (scan_h>? OR (first_scan IS NULL AND NOT delivered)) AS late
            FROM analysis_steps WHERE shipped<=? - INTERVAL '48 hours'
          ) s GROUP BY 1,2,3,4
        )
        SELECT * EXCLUDE(samples),list_slice(samples,1,3) AS examples FROM groups
        WHERE affected>=? AND affected*1.0/total>=0.03 ORDER BY affected DESC,period DESC LIMIT 12
    """, [SCAN_HOURS, asof, 1 if summary["total"] <= 10 else 3])
    stale = _rows(con, """
        SELECT date_trunc('day',last_seen) AS period,carrier_name,last_place AS place,
          CASE last_node WHEN 'hub' THEN '到达区域枢纽' WHEN 'station' THEN '到达派送站'
                         WHEN 'info' THEN '电子信息或待接收'
                         ELSE coalesce(latest_sub,official,'状态未提供') END AS stage,
          count(*) AS affected,count(*) AS open,min(last_seen) AS first_seen,max(last_seen) AS last_seen,
          round(min(date_diff('second',last_seen,?)/3600.0),1) AS hours,
          list_slice(list(id ORDER BY last_seen,id),1,3) AS examples
        FROM analysis_steps WHERE NOT delivered AND last_seen<=? - to_hours(?)
        GROUP BY 1,2,3,4 HAVING count(*)>=?
        ORDER BY affected DESC,period DESC LIMIT 12
    """, [asof, asof, STALE_HOURS, 1 if summary["total"] <= 10 else 3])
    station = _rows(con, """
        WITH daily AS (
          SELECT shipped::DATE AS period,warehouse,carrier_name,channel,hub AS place,
                 count(*) AS total,count(*) FILTER (WHERE station_h>?) AS affected,
                 round(median(station_h),1) AS hours,
                 list_slice(list(id ORDER BY station_h DESC,id) FILTER (WHERE station_h>?),1,3) AS examples
          FROM analysis_steps WHERE station_h IS NOT NULL AND hub IS NOT NULL
            AND shipped<=? - INTERVAL '5 days' GROUP BY 1,2,3,4,5
        ), compared AS (
          SELECT d.*, b.baseline_total,b.baseline_hours,b.baseline_affected
          FROM daily d JOIN LATERAL (
            SELECT sum(total) AS baseline_total,round(median(hours),1) AS baseline_hours,
                   sum(affected) AS baseline_affected
            FROM daily b WHERE b.period BETWEEN d.period-8 AND d.period-1
              AND (b.warehouse,b.carrier_name,b.channel,b.place) = (d.warehouse,d.carrier_name,d.channel,d.place)
          ) b ON TRUE
        )
        SELECT * FROM compared WHERE total>=20 AND baseline_total>=40
          AND hours>=baseline_hours+12
          AND affected*1.0/total>=baseline_affected*1.0/baseline_total+0.20
        ORDER BY affected DESC,period DESC LIMIT 12
    """, [STATION_HOURS, STATION_HOURS, asof])
    explicit = _rows(con, """
        WITH distinct_hits AS (
          SELECT DISTINCT e.id,event_at::DATE AS period,issue,carrier_name,delivered
          FROM analysis_events e JOIN analysis_steps s USING(id) WHERE issue IS NOT NULL AND event_at IS NOT NULL
        )
        SELECT period,carrier_name,issue,count(*) AS affected,count(*) FILTER (WHERE NOT delivered) AS open,
          list_slice(list(id ORDER BY delivered,id),1,3) AS examples
        FROM distinct_hits GROUP BY 1,2,3 HAVING count(*)>=?
        ORDER BY affected DESC,period DESC LIMIT 12
    """, [1 if summary["total"] <= 10 else 3])
    return core.clean({"asOf": asof, "summary": summary, "groups": [
        {"name": key, "label": SIGNALS[key], "rows": rows}
        for key,rows in zip(SIGNALS, (first_scans, stale, station, explicit))
    ]})
