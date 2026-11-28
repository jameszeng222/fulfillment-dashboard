"""Build the complete trajectory download for the selected alert waybills."""
import csv
from datetime import timedelta
from io import BytesIO, StringIO, TextIOWrapper
from itertools import groupby
import re
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.responses import Response

from . import core, tracking_archive
from .tracking_clock import carrier_wall_time
from .waybill import normalized


def csv_cell(value):
    text = "" if value is None else str(value)
    if re.match(r"^\s*[=+@-]", text) or re.fullmatch(r"[0-9]{15,}", text):
        return "'" + text
    return text


def csv_content(headers, rows):
    output = StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    writer.writerows(map(csv_cell, row) for row in rows)
    return output.getvalue().encode("utf-8-sig")


def download(content, filename, *, events=None, details="预警明细.csv"):
    if events is None:
        extension, media_type = ".csv", "text/csv"
    else:
        content = bundle(content, events, details)
        extension, media_type = ".zip", "application/zip"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename + extension, safe='')}",
        "Cache-Control": "no-store",
    })


def _event_rows(con, alerts):
    pairs = sorted({(row["order_no"], str(row["tracking_no"] or "").strip()) for row in alerts})
    if not pairs:
        return
    order_nos, waybills = zip(*pairs)
    con.execute("""
        CREATE TEMP TABLE export_waybills AS
        WITH chosen AS (
            SELECT unnest(?::VARCHAR[]) AS id, unnest(?::VARCHAR[]) AS waybill,
                   unnest(?::VARCHAR[]) AS number
        )
        SELECT DISTINCT p.id,coalesce(nullif(p.waybill,''),o.快递单号,'') AS waybill,
               CASE WHEN p.waybill='' THEN o.单号归一 ELSE nullif(p.number,'') END AS number
        FROM chosen p LEFT JOIN fct_order o ON p.id=o.履约单号
    """, [list(order_nos), list(waybills), [normalized(no) for no in waybills]])
    sources = []
    native = core.columns(con, "trk_event")
    archived = core.columns(con, tracking_archive.EVENT_TABLE)
    if native:
        provider_name = "e.事件承运商" if "事件承运商" in native else "NULL"
        provider_code = "e.事件承运商代码" if "事件承运商代码" in native else "NULL"
        clock = "e.当地时间" if "当地时间" in native else "NULL"
        status_join = ""
        if "运输商代码" in native:
            status_join = """LEFT JOIN trk_status s ON s.单号=p.number
                             WHERE e.运输商代码=coalesce(s.carrier,0) OR e.运输商代码 IS NULL"""
        sources.append(f"""
            SELECT p.id,p.waybill,e.事件时间 AS utc_at,{clock} AS wall,
                   NULL AS local_raw,NULL AS iso,e.主状态 AS stage,e.子状态 AS sub,
                   e.描述 AS description,e.地点 AS place,{provider_name} AS provider,
                   {provider_code} AS provider_code,1 AS source
            FROM export_waybills p JOIN trk_event e ON e.单号=p.number {status_join}
        """)
    if archived and core.columns(con, tracking_archive.TABLE):
        iso = "e.event_time_iso" if "event_time_iso" in archived else "NULL"
        sources.append(f"""
            SELECT DISTINCT p.id,p.waybill,timezone('UTC',try_cast(e.event_time_utc AS TIMESTAMPTZ)),
                   NULL,e.event_time_raw,{iso},e.stage,e.substatus,e.description,e.location,
                   e.provider_name,e.provider_code,0
            FROM export_waybills p JOIN {tracking_archive.TABLE} h
              ON h.履约单号=p.id AND h.单号=p.number AND h.回补状态='tracked'
            JOIN {tracking_archive.EVENT_TABLE} e ON e.tracking_no=p.number AND e.carrier_code=h.carrier
        """)
    if not sources:
        return
    cursor = con.execute(" UNION ALL ".join(sources) + " ORDER BY 1,2,3 NULLS LAST,12 DESC")
    while batch := cursor.fetchmany(4096):
        yield from batch


def _write_track_csv(stream, events):
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
    writer.writerow(["履约单号", "运单号", "节点当地时间", "节点时间（北京时间）", "节点时间（UTC）",
                     "主状态", "子状态", "轨迹描述", "轨迹地点", "事件承运商", "事件承运商代码", "数据来源"])
    for (order_no, waybill), rows in groupby(events, key=lambda row: row[:2]):
        timeline = []
        native_keys = set()
        for _, _, utc_at, wall, local_raw, iso, stage, sub, description, place, provider, code, source in rows:
            local = wall or carrier_wall_time(local_raw, iso)
            key = (utc_at or local, *(str(value or "").strip() for value in (sub, description, place, provider)))
            if source:
                native_keys.add(key)
            timeline.append((key, utc_at, local, stage, sub, description, place, provider, code, source))
        for key, utc_at, local, stage, sub, description, place, provider, code, source in timeline:
            if not source and key in native_keys:
                continue
            beijing = utc_at + timedelta(hours=8) if utc_at else None
            writer.writerow(map(csv_cell, (order_no, waybill, local, beijing, utc_at, stage, sub,
                                          description, place, provider, code, "近期轨迹" if source else "历史轨迹")))


def bundle(detail_csv, alerts, details="预警明细.csv"):
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=4) as archive:
        archive.writestr(details, detail_csv)
        with core.duck() as con, archive.open("轨迹明细.csv", "w") as file:
            with TextIOWrapper(file, encoding="utf-8-sig", newline="") as stream:
                _write_track_csv(stream, _event_rows(con, alerts))
    return output.getvalue()
