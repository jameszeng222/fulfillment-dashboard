"""Prefer carrier delivery instants, with a warehouse-aware WMS fallback."""
from . import core
from .order_time import wms_clock_basis
from .tracking_archive import TABLE as ARCHIVE


DELIVERED_UTC = '"17TRACK妥投UTC"'
DELIVERED_BJ = '"统计妥投北京时间"'
DELIVERY_SOURCE = '"妥投统计来源"'
WMS_CLOCK = '"WMS时间口径"'


def elapsed_minutes(start, end):
    # Compare elapsed time, including seconds at SLA boundaries.
    return f"""CASE WHEN {end}>={start}
                THEN date_diff('microsecond',{start},{end}) / 60000000.0 END"""


def reporting_facts(con, facts):
    event_fields = core.columns(con, "trk_event")
    if event_fields:
        same_carrier = " AND (e.运输商代码=s.carrier OR e.运输商代码 IS NULL)" if "运输商代码" in event_fields else ""
        native = f"""
            SELECT s.单号,s.carrier,greatest(s.签收时间,max(e.事件时间)) AS delivered
            FROM trk_status s LEFT JOIN trk_event e
              ON e.单号=s.单号 {same_carrier}
             AND (e.主状态='Delivered' OR starts_with(e.子状态,'Delivered_'))
            GROUP BY s.单号,s.carrier,s.签收时间
            HAVING delivered IS NOT NULL
        """
    else:
        native = "SELECT 单号,carrier,签收时间 AS delivered FROM trk_status WHERE 签收时间 IS NOT NULL"

    archive_join = ""
    instant = "n.delivered"
    if "签收时间" in core.columns(con, ARCHIVE):
        archive_join = f"""
            LEFT JOIN (
                SELECT 履约单号,单号,max(carrier) AS carrier,max(签收时间) AS delivered
                FROM {ARCHIVE} WHERE 回补状态='tracked' AND 签收时间 IS NOT NULL
                GROUP BY 履约单号,单号 HAVING count(DISTINCT carrier)<=1
            ) h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
        """
        instant = """CASE WHEN h.delivered IS NOT NULL AND h.carrier IS DISTINCT FROM n.carrier
                      THEN h.delivered ELSE greatest(h.delivered,n.delivered) END"""
    return f"""(
        SELECT f.* EXCLUDE (物流分钟, wms_valid),
               coalesce(f.{DELIVERED_UTC} + INTERVAL '8 hours',f.wms_valid) AS {DELIVERED_BJ},
               CASE WHEN f.{DELIVERED_UTC} IS NOT NULL THEN '17TRACK'
                    WHEN f.wms_valid IS NOT NULL THEN 'WMS' END AS {DELIVERY_SOURCE},
               {elapsed_minutes("f.签出时间", f"coalesce(f.{DELIVERED_UTC} + INTERVAL '8 hours',f.wms_valid)")} AS 物流分钟
        FROM (
            SELECT j.* EXCLUDE (carrier_instant, wms_instant),
                   CASE WHEN coalesce(j.支付时间,j.创建时间) IS NULL OR
                             j.carrier_instant + INTERVAL '8 hours'>=coalesce(j.支付时间,j.创建时间)
                        THEN j.carrier_instant END AS {DELIVERED_UTC},
                   CASE WHEN coalesce(j.签出时间,j.创建时间,j.支付时间) IS NULL OR
                             j.wms_instant>=coalesce(j.签出时间,j.创建时间,j.支付时间)
                        THEN j.wms_instant END AS wms_valid
            FROM (
                SELECT classified.*,
                       CASE classified.{WMS_CLOCK}
                            WHEN '北京时间' THEN classified.签收时间
                            WHEN '目的地当地时间' THEN classified.签收北京时间
                       END AS wms_instant
                FROM (
                    SELECT o.*, {instant} AS carrier_instant, {wms_clock_basis("o")} AS {WMS_CLOCK}
                    FROM {facts} o LEFT JOIN ({native}) n ON n.单号=o.单号归一
                    {archive_join}
                ) classified
            ) j
        ) f
    )"""
