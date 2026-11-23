"""四个业务页面的数据接口：总览 / 轨迹监控 / 渠道时效 / 规则。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from typing import Literal
from unicodedata import normalize

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
import pandas as pd

from . import alert_history, core, public_preview, rules
from .carrier_delivery import (
    DELIVERY_SOURCE, DELIVERED_BJ, DELIVERED_UTC, WMS_CLOCK, elapsed_minutes, reporting_facts, transport_minutes,
)
from .channel_timing import ONLINE_START, TimingBasis, online_scan_join, timing_start, transport_archive_version
from .filter_time import time_bounds, week_filter
from .order_time import delivery_facts
from .tracking_archive import fill_local_times, sources as tracking_sources
from .waybill import key_sql, normalized as normalize_waybill

router = APIRouter(prefix="/api")

TRACK_STATUS_LABEL = """coalesce(t.状态中文, CASE
    WHEN m.单号 IS NOT NULL THEN 'API 账号登记未匹配'
    WHEN h.回补状态='empty' THEN '已查询 · 未返回轨迹'
    WHEN o.已签收=1 THEN '已签收 · 轨迹未入库'
    WHEN o.签出时间 IS NOT NULL THEN '已出库 · 待同步轨迹'
    ELSE '其他履约单 · 无本地结果' END)"""


# ---------------------------------------------------------------- 公共

def _cond(month="", wh="", pf="", cr="", cn="", status="", start="", end="", q="",
          alias="o", lastmile="", channel="", date_basis="created", week=""):
    """返回 (sql, args)，sql 形如 " AND ..."，可直接拼在 WHERE 1=1 之后。"""
    w, a = [], []

    def inl(column, value, parse=core.filter_values):
        vals = parse(value)
        if vals:
            w.append(f'{column} IN ({",".join("?" * len(vals))})')
            a.extend(vals)

    timestamp_column = f"{alias}.签出时间" if date_basis == "shipped" else f"{alias}.创建时间"
    date_column = f"CAST({timestamp_column} AS DATE)" if date_basis == "shipped" else f"{alias}.创建日期"
    month_column = f"strftime({date_column}, '%Y-%m')" if date_basis == "shipped" else f"{alias}.创建月份"
    inl(month_column, month)
    week_sql, week_args = week_filter(date_column, core.filter_values(week))
    if week_sql:
        w.append(week_sql)
        a.extend(week_args)
    for column, value in (("仓库", wh), ("平台", pf), ("物流商", cr),
                          ("目的国", cn), ("状态", status), ("当前渠道", channel)):
        inl(f'{alias}."{column}"', value)
    inl(f"coalesce(nullif({alias}.C端承运商,''),'__empty__')", lastmile, core.last_mile_filter_values)
    for operator, bound in zip((">=", "<"), time_bounds(start, end)):
        if bound is not None:
            column = timestamp_column if isinstance(bound, datetime) else date_column
            w.append(f"{column} {operator} ?")
            a.append(bound)
    query = normalize("NFKC", q).strip().upper()
    if query:
        columns = ("履约单号", "快递单号", "订单号", "平台订单号")
        searches = [f"contains(upper({alias}.{column}), ?)" for column in columns]
        a.extend([query] * len(columns))
        number = re.sub(r"[^A-Z0-9]", "", query)
        if number:
            tracking_number = normalize_waybill(query)
            if tracking_number:
                searches.append(f"contains({alias}.单号归一, ?)")
                a.append(tracking_number)
                if len(tracking_number) >= 8:
                    searches.append(f"""{alias}.履约单号 IN (
                        SELECT x.履约单号 FROM 履约单明细 x WHERE {key_sql("x.快递单号")}=?
                    )""")
                    a.append(tracking_number)
            if number != query:
                searches.extend(f"upper({alias}.{column}) = ?" for column in ("履约单号", "订单号"))
                a.extend([number] * 2)
        w.append("(" + " OR ".join(searches) + ")")
    return (" AND " + " AND ".join(w)) if w else "", a


# ---------------------------------------------------------------- 总览

def _warehouse_shipping_mix(con):
    result = con.execute("""
        WITH shipped AS (
            SELECT upper(trim(仓库)) AS warehouse FROM overview_timing
            WHERE 签出时间 IS NOT NULL
        ), regions AS (
            SELECT CASE
                WHEN contains(warehouse,'WINIT') OR warehouse IN ('MGZYTH01','NF01') THEN 'overseas'
                WHEN warehouse IN ('JY01','JY02','JY03','XC01','XCWH01','WH01','ZBJ') THEN 'domestic'
                ELSE 'unknown' END AS region
            FROM shipped
        )
        SELECT count(*) AS total, count(*) FILTER (WHERE region='overseas') AS overseas,
               count(*) FILTER (WHERE region='domestic') AS domestic,
               count(*) FILTER (WHERE region='unknown') AS unknown
        FROM regions
    """)
    return dict(zip((column[0] for column in result.description), result.fetchone()))


def _order_split_mix(con, active_orders, filtered):
    if filtered:
        # Use the entire order to classify splits, even if only one fulfillment matches.
        order_source = f"""{active_orders} o SEMI JOIN (
            SELECT DISTINCT coalesce(trim(平台),'') AS platform, trim(订单号) AS order_no
            FROM overview_timing WHERE nullif(trim(订单号),'') IS NOT NULL
        ) matched ON matched.platform=coalesce(trim(o.平台),'') AND matched.order_no=trim(o.订单号)"""
    else:
        order_source = "overview_timing o"
    result = con.execute(f"""
        WITH order_groups AS (
            SELECT coalesce(trim(o.平台),'') AS platform, trim(o.订单号) AS order_no,
                   count(*) AS fulfillments,
                   count(nullif(trim(o.仓库),'')) AS known_warehouses,
                   min(nullif(upper(trim(o.仓库)),'')) AS first_warehouse,
                   max(nullif(upper(trim(o.仓库)),'')) AS last_warehouse
            FROM {order_source}
            WHERE nullif(trim(o.订单号),'') IS NOT NULL GROUP BY 1,2
        ), kinds AS (
            SELECT CASE
                WHEN fulfillments=1 THEN 'single'
                WHEN first_warehouse<>last_warehouse THEN 'cross'
                WHEN known_warehouses=fulfillments THEN 'same'
                ELSE 'unknown' END AS kind
            FROM order_groups
        )
        SELECT count(*) AS total, count(*) FILTER (WHERE kind<>'single') AS split,
               count(*) FILTER (WHERE kind='same') AS same, count(*) FILTER (WHERE kind='cross') AS cross,
               count(*) FILTER (WHERE kind='unknown') AS unknown,
               (SELECT count(*) FROM overview_timing WHERE nullif(trim(订单号),'') IS NULL) AS missingFulfillments
        FROM kinds
    """)
    return dict(zip((column[0] for column in result.description), result.fetchone()))


@router.get("/overview")
def overview(request: Request, month: str = "", wh: str = "", pf: str = "", cr: str = "",
             cn: str = "", start: str = "", end: str = "", lastmile: str = "", channel: str = "", week: str = ""):
    core.require_reader(request)
    return _overview(core.snapshot_key(), month, wh, pf, cr, cn, start, end, lastmile, channel, week)


@lru_cache(maxsize=32)
def _overview(_snapshot, month, wh, pf, cr, cn, start, end, lastmile, channel, week):
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, lastmile=lastmile, channel=channel, week=week)

    con = core.duck()
    try:
        all_orders = core.order_facts(con)
        active_orders = core.active_facts(all_orders)
        order_facts = delivery_facts(con, active_orders)
        facts = reporting_facts(con, order_facts)
        tracking, missing, archive = tracking_sources(con)
        con.execute(f"""
            CREATE TEMP TABLE overview_timing AS
            SELECT o.创建月份,o.仓内分钟,o.物流分钟,o.件数,o.仓库,o.物流商,o.目的国中文,
                   o.C端承运商,o.当前渠道,o.签出时间,o.平台,o.订单号,o.{DELIVERED_BJ},o.{DELIVERY_SOURCE}
            FROM {facts} o WHERE 1=1{cond}
        """, args)
        k = con.execute(f"""
            SELECT count(*) n, coalesce(sum(件数),0) items, count({DELIVERED_BJ}) sign,
                   avg(仓内分钟) FILTER (WHERE 仓内分钟 > 0) hm,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0) tm,
                   count(*) FILTER (WHERE 物流分钟 > 0) sgn,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 4320) d3,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 7200) d5,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 8640) d6,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 10080) d7,
                   count(*) FILTER (WHERE {DELIVERY_SOURCE}='WMS') fallback_signed,
                   count(*) FILTER (WHERE {DELIVERY_SOURCE}='WMS' AND 物流分钟>0) fallback_samples
            FROM overview_timing
        """).fetchone()
        warehouse_shipping = _warehouse_shipping_mix(con)
        order_split = _order_split_mix(con, active_orders, bool(cond))

        def by(col, limit=20):
            return con.execute(f"""
                SELECT o."{col}" k, count(*) n, count(o.{DELIVERED_BJ}) s,
                       avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0)/1440.0 d
                FROM overview_timing o GROUP BY 1 ORDER BY n DESC, k NULLS LAST LIMIT {limit}
            """).df().to_dict("records")

        monthly = con.execute(f"""
            SELECT o.创建月份 m, count(*) n, count(o.{DELIVERED_BJ}) s,
                   avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0)/1440.0 d
            FROM overview_timing o GROUP BY 1 ORDER BY 1
        """).df().to_dict("records")
        daily = con.execute(f"""
            SELECT o.创建日期::VARCHAR AS day, count(*) AS n
            FROM {order_facts} o WHERE o.创建日期 IS NOT NULL{cond}
            GROUP BY 1 ORDER BY 1
        """, args).fetchall()
        ship_cond, ship_args = _cond(month, wh, pf, cr, cn, start=start, end=end,
                                    lastmile=lastmile, channel=channel, date_basis="shipped", week=week)
        daily_shipped = con.execute(f"""
            SELECT CAST(o.签出时间 AS DATE)::VARCHAR AS day, count(*) AS n
            FROM {active_orders} o WHERE o.签出时间 IS NOT NULL{ship_cond}
            GROUP BY 1 ORDER BY 1
        """, ship_args).fetchall()

        by_carrier = by("物流商", 20)
        by_wh = by("仓库", 20)
        by_country = by("目的国中文", 15)
        by_status = con.execute(f"""
            SELECT coalesce(o.状态, '未填写状态') k, count(*) n
            FROM {all_orders} o WHERE 1=1{cond} GROUP BY 1 ORDER BY n DESC,k
        """, args).df().to_dict("records")
        by_last_mile = by("C端承运商", 16)
        by_current_channel = by("当前渠道", 10)
        track_states = con.execute(f"""
            SELECT {TRACK_STATUS_LABEL} k, count(*) n
            FROM {order_facts} o
            LEFT JOIN {tracking} t ON t.单号=o.单号归一
            LEFT JOIN {missing} m ON m.单号=o.单号归一
            LEFT JOIN {archive} h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
            WHERE 1=1{cond}
            GROUP BY 1 ORDER BY 2 DESC
        """, args).df().to_dict("records")
        asof = con.execute(f"SELECT max(创建日期) FROM {active_orders}").fetchone()[0]
    finally:
        con.close()

    n = k[0] or 0
    sgn = k[5] or 0

    def rate(x):
        return round(100.0 * x / sgn, 1) if sgn else None

    return core.clean({
        "asof": str(asof),
        "kpi": {
            "orders": n, "items": int(k[1] or 0),
            "signRate": round(100.0 * (k[2] or 0) / n, 1) if n else None,
            "inboundHours": round(k[3] / 60.0, 2) if k[3] else None,
            "transitDays": round(k[4] / 1440.0, 2) if k[4] else None,
            "transitSamples": sgn,
            "rate3": rate(k[6]), "rate5": rate(k[7]), "rate6": rate(k[8]), "rate7": rate(k[9]),
            "signed": int(k[2] or 0),
            "fallbackSigned": k[10], "fallbackTransitSamples": k[11],
        },
        "monthly": monthly, "byCarrier": by_carrier, "byWarehouse": by_wh,
        "warehouseShipping": warehouse_shipping, "orderSplit": order_split,
        "daily": [{"day": day, "n": count} for day, count in daily],
        "dailyShipped": [{"day": day, "n": count} for day, count in daily_shipped],
        "byCountry": by_country, "byStatus": by_status,
        "byLastMile": by_last_mile, "byCurrentChannel": by_current_channel, "byTrackingStatus": track_states,
    })


# ---------------------------------------------------------------- 轨迹监控

@router.get("/track")
def track(request: Request,
          page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
          q: str = "", month: str = "", wh: str = "", pf: str = "", cr: str = "",
          cn: str = "", tstat: str = "", rule: str = "", signed: str = "",
          sort: str = "停滞天数", order: str = "desc",
          start: str = "", end: str = "", lastmile: str = "",
          sub: str = "", coverage: str = "", channel: str = "", week: str = "",
          date_basis: Literal["", "created", "shipped"] = "created",
          part: Literal["all", "rows", "summary"] = "all"):
    if core.require_reader(request)["preview"]:
        return public_preview.search(q, page, size)
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, q=q, lastmile=lastmile, channel=channel,
                       date_basis=date_basis, week=week)

    for column, value in (("t.状态", tstat), ("t.子状态", sub), ("a.主预警规则", rule)):
        vals = core.filter_values(value)
        if vals:
            cond += f' AND {column} IN ({",".join("?" * len(vals))})'
            args.extend(vals)
    if coverage == "canceled_unstored":
        cond += " AND FALSE"
    if coverage in ("tracked", "unmatched", "queried_empty", "unknown", "signed_unstored",
                    "shipped_unstored", "other_unstored"):
        cond += {
            "tracked": " AND (t.单号 IS NOT NULL OR h.回补状态='empty')",
            "unmatched": " AND t.单号 IS NULL AND m.单号 IS NOT NULL",
            "queried_empty": " AND t.单号 IS NULL AND m.单号 IS NULL AND h.回补状态='empty'",
            "unknown": " AND t.单号 IS NULL AND m.单号 IS NULL AND h.回补状态 IS NULL",
            "signed_unstored": " AND t.单号 IS NULL AND m.单号 IS NULL AND h.回补状态 IS NULL AND o.已签收=1",
            "shipped_unstored": " AND t.单号 IS NULL AND m.单号 IS NULL AND h.回补状态 IS NULL AND o.已签收=0 AND o.签出时间 IS NOT NULL",
            "other_unstored": " AND t.单号 IS NULL AND m.单号 IS NULL AND h.回补状态 IS NULL AND o.已签收=0 AND o.签出时间 IS NULL",
        }[coverage]
    if signed in ("0", "1"):
        cond += " AND o.已签收 = ?"; args.append(int(signed))

    sort_cols = {"停滞天数": "t.停滞天数", "签出时间": "o.签出时间", "创建时间": "o.创建时间",
                 "物流分钟": "o.物流分钟", "履约单号": "o.履约单号", "逐条事件数": "逐条事件数"}
    sc = sort_cols.get(sort, "t.停滞天数")
    direction = "ASC" if order.lower() == "asc" else "DESC"
    off = (page - 1) * size

    con = core.duck()
    try:
        facts = delivery_facts(con, core.active_facts(core.order_facts(con)))
        order_only = not any((tstat, sub, rule, coverage))
        fast_page = part == "rows" and order_only and sc in ("o.签出时间", "o.创建时间", "o.履约单号")
        search_total = None
        if fast_page:
            # Select the visible orders before joining carrier status and alerts.
            page_cond, page_args = cond, args
            if q.strip():
                con.execute(f"""
                    CREATE TEMP TABLE matching_track_orders AS
                    SELECT o.履约单号 FROM {facts} o WHERE 1=1{cond}
                """, args)
                search_total = con.execute("SELECT count(*) FROM matching_track_orders").fetchone()[0]
                if off >= search_total:
                    return {"total": search_total, "page": page, "size": size, "rows": [],
                            "statusSummary": [], "ruleSummary": []}
                page_cond, page_args = " AND o.履约单号 IN (SELECT 履约单号 FROM matching_track_orders)", []
            con.execute(f"""
                CREATE TEMP TABLE selected_track_orders AS
                SELECT o.* FROM {facts} o WHERE 1=1{page_cond}
                ORDER BY {sc} {direction} NULLS LAST, o.履约单号
                LIMIT ? OFFSET ?
            """, [*page_args, size, off])
        numbers = "SELECT 单号归一 FROM selected_track_orders" if fast_page else None
        tracking, missing, archive = tracking_sources(con, numbers)
        alert_where, alert_args = rules.visible_alerts(), []
        if fast_page:
            alert_args = [row[0] for row in con.execute("SELECT 履约单号 FROM selected_track_orders").fetchall()]
            alert_where += f' AND order_no IN ({",".join("?" * len(alert_args))})' if alert_args else " AND FALSE"
        with core.ops() as ops:
            summaries = ops.execute(f"""
                WITH ranked AS (
                    SELECT order_no, rule_cn, rule, level, count(*) OVER by_order AS total,
                           row_number() OVER (by_order ORDER BY is_main DESC,
                              CASE level WHEN '紧急' THEN 0 WHEN '高' THEN 1 ELSE 2 END, rule) AS choice
                    FROM ({alert_history.current_rows()})
                    WHERE {alert_where} AND state IN {alert_history.PENDING_STATES}
                    WINDOW by_order AS (PARTITION BY order_no)
                )
                SELECT order_no,rule_cn,rule,level,total FROM ranked WHERE choice=1
            """, alert_args).fetchall()
        con.register("live_alerts", pd.DataFrame.from_records(
            summaries, columns=["履约单号", "主预警名称", "主预警规则", "主预警级别", "预警数"]
        ).astype({"履约单号": "string", "主预警名称": "string", "主预警规则": "string",
                  "主预警级别": "string", "预警数": "int64"}))
        event_columns = core.columns(con, "trk_event")
        event_key = "e.单号=o.单号归一"
        if "运输商代码" in event_columns:
            event_key += " AND (e.运输商代码 = coalesce(t.carrier, 0) OR e.运输商代码 IS NULL)"
        if coverage == "detailed":
            native = f" OR EXISTS (SELECT 1 FROM trk_event e WHERE {event_key})" if event_columns else ""
            cond += f" AND (h.事件数>0{native})"
        native_count = f"(SELECT count(*) FROM trk_event e WHERE {event_key})" if event_columns else "0"
        event_count = f"coalesce(nullif(h.事件数,0),{native_count})"
        joins = (f"LEFT JOIN {tracking} t ON t.单号 = o.单号归一 "
                f"LEFT JOIN {missing} m ON m.单号 = o.单号归一 "
                f"LEFT JOIN {archive} h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一 "
                "LEFT JOIN live_alerts a ON a.履约单号 = o.履约单号")
        joined = joins + " WHERE 1=1" + cond
        if part != "rows":
            summary = con.execute(f"""
                SELECT grouping(状态) AS by_rule, coalesce(状态,异常) AS label, count(*) AS n
                FROM (SELECT {TRACK_STATUS_LABEL} 状态, coalesce(a.主预警名称,'无异常') 异常 FROM {facts} o {joined})
                GROUP BY GROUPING SETS ((状态),(异常))
                ORDER BY n DESC,label
            """, args).fetchall()
            status_summary = [{"状态": label, "n": count} for by_rule, label, count in summary if not by_rule]
            rule_summary = [{"异常": label, "n": count} for by_rule, label, count in summary if by_rule]
            total = sum(row["n"] for row in status_summary)
            if part == "summary":
                return {"total": total, "statusSummary": status_summary, "ruleSummary": rule_summary}
        else:
            status_summary, rule_summary = [], []
            count_scope = (" WHERE 1=1" + cond) if order_only else joined
            total = search_total
            if total is None:
                total = con.execute(f"SELECT count(*) FROM {facts} o {count_scope}", args).fetchone()[0]

        timed_sort = sc == "o.物流分钟"
        page_facts = reporting_facts(con, facts) if timed_sort else facts
        if fast_page:
            page_facts = "selected_track_orders"
            joined = joins
            args, off = [], 0
        timing_columns = f"o.{DELIVERED_UTC}, o.{DELIVERED_BJ}, o.{DELIVERY_SOURCE}, o.{WMS_CLOCK}," if timed_sort else ""
        # Counting native events for all matching orders is only required when
        # that count determines the sort order. Otherwise count the chosen page.
        order_by_count = sc == "逐条事件数"
        projected_events = f"{event_count} AS 逐条事件数" if order_by_count else "h.事件数 AS archive_events"
        page_query = f"""
            SELECT o.履约单号, o.订单号, o.快递单号, o.单号归一, o.平台, o.仓库, o.物流商, o.C端承运商, o.当前渠道,
                   o.目的国中文, o.状态, o.支付时间, o.创建时间, o.签出时间, o.签收时间,
                   {timing_columns}
                   t.签收时间 AS tracked_delivery,t.签收当地时间 AS tracked_delivery_local,
                   o.签收北京时间, o.签收时区,
                   o.物流分钟, o.已签收,
                   t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
                   t.最新事件时间, t.最新事件当地时间, t.最新描述, t.地点, t.停滞天数,
                   h.carrier AS 回补运输商代码,
                   t.carrier AS 运输商代码, coalesce(t.查询时间,h.查询时间) AS 查询时间, h.回补状态,
                   {projected_events},
                   a.主预警名称, a.主预警规则, a.主预警级别, a.预警数,
                   (t.单号 IS NULL AND m.单号 IS NOT NULL) AS 未注册
            FROM {page_facts} o {joined}
            ORDER BY {sc} {direction} NULLS LAST, o.履约单号
            LIMIT ? OFFSET ?
        """
        if not order_by_count:
            native_on_page = "0"
            if event_columns:
                match = "e.单号=p.单号归一"
                if "运输商代码" in event_columns:
                    match += " AND (e.运输商代码=coalesce(p.运输商代码,0) OR e.运输商代码 IS NULL)"
                native_on_page = f"(SELECT count(*) FROM trk_event e WHERE {match})"
        measured = "page_rows" if timed_sort else reporting_facts(con, "page_rows", page_only=True)
        excluded = [DELIVERED_UTC, DELIVERED_BJ, "tracked_delivery", "tracked_delivery_local"]
        event_projection = ""
        if not order_by_count:
            excluded.append("archive_events")
            event_projection = f", coalesce(nullif(p.archive_events,0),{native_on_page}) AS 逐条事件数"
        sort_field = sc.split(".")[-1]
        page_query = f"""
            WITH page_rows AS MATERIALIZED ({page_query})
            SELECT p.* EXCLUDE ({', '.join(excluded)}),
                   p.{DELIVERED_UTC} AS 承运商签收时间,
                   CASE WHEN p.tracked_delivery=p.{DELIVERED_UTC} THEN p.tracked_delivery_local END AS 签收当地时间,
                   {transport_minutes('p', "p.轨迹状态 IS NOT DISTINCT FROM 'Delivered'")} AS 运输分钟
                   {event_projection}
            FROM {measured} p ORDER BY p."{sort_field}" {direction} NULLS LAST, p.履约单号
        """
        rows = con.execute(page_query, args + [size, off]).df().to_dict("records")
        fill_local_times(con, rows)
        with core.ops() as ops:
            resolved = alert_history.resolved_for_orders(ops, [row["履约单号"] for row in rows])
        for row in rows:
            row["历史预警"] = resolved.get(row["履约单号"])

    finally:
        con.close()

    return core.clean({"total": int(total), "page": page, "size": size,
                       "rows": rows,
                       "statusSummary": status_summary, "ruleSummary": rule_summary})


# ---------------------------------------------------------------- 渠道时效

@router.get("/channels")
def channels(request: Request, month: str = "", wh: str = "", pf: str = "",
             cn: str = "", min_n: int = Query(100, ge=1), cr: str = "", lastmile: str = "",
             start: str = "", end: str = "", channel: str = "", week: str = "",
             basis: TimingBasis = "shipped"):
    core.require_reader(request)
    if basis == "created":
        basis = "paid"  # Value used by earlier clients for 下单到签收.
    with core.ops() as ops:
        _, saved = rules.load_settings(ops)
    archive = transport_archive_version() if basis == "online" or any(
        item.get("basis") == "online" for item in saved.values()
    ) else None
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, lastmile=lastmile, channel=channel, week=week)
    report = _channel_report(core.snapshot_key(), archive, json.dumps(saved, sort_keys=True), basis, cond, tuple(args))
    return {**report, "rows": [row for row in report["rows"] if row["orders"] >= min_n]}


@lru_cache(maxsize=32)
def _channel_report(_snapshot, _archive, saved, basis, cond, args):
    con = core.duck()
    try:
        raw_facts = core.active_facts(core.order_facts(con))
        facts = reporting_facts(con, delivery_facts(con, raw_facts))
        asof = con.execute(f"SELECT max(创建日期) FROM {raw_facts}").fetchone()[0]
        slas = rules.current_channel_slas(con, raw_facts, json.loads(saved))
        sla_sql, sla_values = rules.sla_table(slas)
        sla_scans = any(config["basis"] == "online" for config in slas.values())
        scan_join = online_scan_join(con) if basis == "online" or sla_scans else ""
        minutes = {
            "paid": elapsed_minutes("o.支付时间", f"o.{DELIVERED_BJ}"),
            "shipped": "o.物流分钟",
            "online": elapsed_minutes(ONLINE_START, f"o.{DELIVERED_BJ}"),
        }[basis]
        sla_start = timing_start("tgt.basis", sla_scans)
        active = f"o.已签收=0 AND o.{DELIVERED_UTC} IS NULL AND o.状态 IS DISTINCT FROM '已完成' AND o.签出时间 IS NOT NULL"
        scoped = f"""SELECT coalesce(nullif(o.C端承运商,''),'__empty__') AS key,
                            coalesce(o.目的国中文,o.目的国) AS ctry,
                            o.{DELIVERED_BJ} AS delivered, o.{DELIVERY_SOURCE} AS delivery_source,
                            {minutes} AS duration_min, {sla_start} AS sla_start,
                            coalesce(tgt.days,{rules.DEFAULT_SLA_DAYS}) AS sla_days,
                            {active} AS in_transit
                     FROM {facts} o LEFT JOIN tgt ON tgt.ch=o.当前渠道
                     {scan_join} WHERE 1=1{cond}"""
        usable = "o.duration_min>0"
        sla_minutes = elapsed_minutes("o.sla_start", "o.delivered")
        sla_usable = f"{sla_minutes}>0"
        grouped = con.execute(f"""
            WITH tgt(ch, days, basis) AS ({sla_sql}), scoped AS ({scoped})
            SELECT grouping(o.ctry) AS channel_total, o.key, o.ctry, count(*) AS orders,
                   count(o.delivered) AS signed,
                   count(*) FILTER (WHERE {usable}) AS samples,
                   count(*) FILTER (WHERE {usable} AND o.delivery_source='WMS') AS fallbackSamples,
                   round(avg(o.duration_min) FILTER (WHERE {usable}) / 1440.0, 2) AS avgDays,
                   round(quantile_cont(o.duration_min, 0.9) FILTER (WHERE {usable}) / 1440.0, 2) AS p90Days,
                   count(*) FILTER (WHERE {usable} AND o.duration_min<=4320) AS d3,
                   count(*) FILTER (WHERE {usable} AND o.duration_min<=7200) AS d5,
                   count(*) FILTER (WHERE {usable} AND o.duration_min<=8640) AS d6,
                   count(*) FILTER (WHERE {usable} AND o.duration_min<=10080) AS d7,
                   count(*) FILTER (WHERE {sla_usable}) AS sla_samples,
                   count(*) FILTER (WHERE {sla_usable} AND {sla_minutes}<=o.sla_days*1440) AS within_sla,
                   count(*) FILTER (WHERE o.in_transit) AS inTransit,
                   count(*) FILTER (WHERE o.in_transit AND o.sla_start IS NOT NULL) AS slaInTransit,
                   count(*) FILTER (WHERE o.in_transit AND
                      date_diff('second',o.sla_start,?::TIMESTAMP)>o.sla_days*86400) AS overdue
            FROM scoped o
            GROUP BY GROUPING SETS ((o.key), (o.key, o.ctry))
            HAVING grouping(o.ctry)=1 OR count(*)>=200
            ORDER BY orders DESC,o.key,o.ctry
        """, [*sla_values, *args, asof]).df().to_dict("records")
    finally:
        con.close()

    def pct(count, total):
        return round(100 * count / total, 1) if total else None

    records, by_country = [], []
    for row in grouped:
        name = "未填写 C 端渠道" if row["key"] == "__empty__" else row["key"]
        if not row.pop("channel_total"):
            if len(by_country) < 40:
                by_country.append({"key": row["key"], "ch": name, "ctry": row["ctry"], "n": row["orders"],
                                   "avgDays": row["avgDays"], "samples": row["samples"],
                                   "rate6": pct(row["d6"], row["samples"])})
            continue
        row.pop("ctry")
        row["channel"] = name
        for day in (3, 5, 6, 7):
            row[f"rate{day}"] = pct(row.pop(f"d{day}"), row["samples"])
        row["slaRate"] = pct(row.pop("within_sla"), row["sla_samples"])
        row["overRate"] = pct(row["overdue"], row["slaInTransit"])
        records.append(row)
    return core.clean({"asof": str(asof), "basis": basis, "rows": records, "byCountry": by_country})


# ---------------------------------------------------------------- 规则与 SLA

@router.get("/rules")
def rules_api(request: Request):
    core.require_user(request)
    with core.ops() as ops:
        rule_overrides, sla_overrides = rules.load_settings(ops)
        hits = dict(ops.execute(
            f"SELECT rule,count(*) FROM alerts WHERE is_main AND {rules.visible_alerts()} GROUP BY rule"
        ).fetchall())
    con = core.duck()
    try:
        raw_facts = core.active_facts(core.order_facts(con))
        facts = reporting_facts(con, delivery_facts(con, raw_facts))
        asof = con.execute(f"SELECT max(创建日期) FROM {raw_facts}").fetchone()[0]
        slas = rules.current_channel_slas(con, raw_facts, sla_overrides)
        sla_sql, sla_values = rules.sla_table(slas)
        have_online = any(config["basis"] == "online" for config in slas.values())
        scan_join = online_scan_join(con) if have_online else ""
        start_at = timing_start("tgt.basis", have_online)
        usable = "duration_min>0"
        perf = con.execute(f"""
            WITH tgt(ch, days, basis) AS ({sla_sql}), scoped AS (
                SELECT o.当前渠道, o.{DELIVERED_BJ}, {elapsed_minutes(start_at, f"o.{DELIVERED_BJ}")} AS duration_min
                FROM {facts} o LEFT JOIN tgt ON tgt.ch=o.当前渠道
                {scan_join} WHERE o.当前渠道 IS NOT NULL
            )
            SELECT 当前渠道 AS ch, count(*) AS n,
                   count({DELIVERED_BJ}) AS signed,
                   count(*) FILTER (WHERE {usable}) AS samples,
                   count(*) FILTER (WHERE {usable} AND duration_min<=4320) AS d3,
                   count(*) FILTER (WHERE {usable} AND duration_min<=7200) AS d5,
                   count(*) FILTER (WHERE {usable} AND duration_min<=8640) AS d6,
                   round(avg(duration_min) FILTER (WHERE {usable}) / 1440.0, 2) AS avgDays
            FROM scoped GROUP BY 1 ORDER BY n DESC,ch
        """, sla_values).df().to_dict("records")
    finally:
        con.close()

    for row in perf:
        config = slas[row["ch"]]
        row.update(target=config["days"], targets=config["targets"], basis=config["basis"],
                   customized=row["ch"] in sla_overrides)
        for day in (3, 5, 6):
            count = row.pop(f"d{day}")
            row[f"rate{day}"] = round(100 * count / row["samples"], 1) if row["samples"] else None
    active_rules = rules.effective_rules(rule_overrides)
    for row in active_rules:
        row["hits"] = hits.get(row["key"], 0)
    return core.clean({
        "asof": str(asof), "rules": active_rules, "notImplemented": rules.NOT_IMPLEMENTED,
        "workdayRule": rules.WORKDAY_RULE, "slaDefault": rules.DEFAULT_SLA_DAYS, "channels": perf,
    })


class RuleConfig(BaseModel):
    enabled: bool
    level: Literal["紧急", "高", "中"]
    threshold: int | None = Field(default=None, ge=0, le=3650)


class SLATargets(BaseModel):
    d3: float = Field(ge=0, le=100)
    d5: float = Field(ge=0, le=100)
    d6: float = Field(ge=0, le=100)


class ChannelSLA(BaseModel):
    channel: str
    days: int = Field(gt=0, le=3650)
    targets: SLATargets
    basis: TimingBasis | None = None


def save_configuration(user, kind, name, config):
    import alerts

    con = core.duck()
    try:
        if kind == "sla" and not con.execute(
            f"SELECT 1 FROM {core.active_facts(core.order_facts(con))} o WHERE 当前渠道=? LIMIT 1", [name]
        ).fetchone():
            raise HTTPException(status_code=404, detail="没有找到这个履约单当前渠道")
        with core.ops() as ops:
            ops.execute("BEGIN IMMEDIATE")
            if kind == "sla" and "basis" not in config:
                _, saved = rules.load_settings(ops)
                config["basis"] = saved.get(name, {}).get("basis", "shipped")
            rules.save_setting(ops, kind, name, config)
            overrides = rules.load_settings(ops)
            hits, orders = alerts.calculate(con, None, *overrides)
            alerts.update_alert_mirror(con, ops)
            core.audit(user["username"], f"configure_{kind}", name, config, connection=ops)
        return {"ok": True, "hits": hits, "orders": orders}
    finally:
        con.close()


@router.put("/rules/business/{key}")
def configure_rule(key: str, body: RuleConfig, request: Request):
    user = core.require_admin(request)
    if key not in rules.RULES_BY_KEY:
        raise HTTPException(status_code=404, detail="没有找到这条规则")
    if key in rules.RULE_THRESHOLDS and body.threshold is None:
        raise HTTPException(status_code=422, detail="请输入预警时效阈值")
    return save_configuration(user, "rule", key, body.model_dump(exclude_none=True))


@router.put("/rules/channel-sla")
def configure_sla(body: ChannelSLA, request: Request):
    user = core.require_admin(request)
    config = body.model_dump(exclude={"channel"}, exclude_none=True)
    if config.get("basis") == "created":
        config["basis"] = "paid"
    return save_configuration(user, "sla", body.channel, config)
