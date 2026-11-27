"""四个业务页面的数据接口：总览 / 轨迹监控 / 渠道时效 / 规则。"""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from functools import lru_cache
from typing import Literal
from unicodedata import normalize

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
import pandas as pd

from . import alert_history, analytics_snapshot, channel_groups, core, ops_assistant, public_preview, rules, us_analysis, track_analysis, track_lookup
from .carrier_delivery import (
    DELIVERY_SOURCE, DELIVERED_BJ, DELIVERED_UTC, WMS_CLOCK, elapsed_minutes, reporting_facts, transport_minutes,
)
from .channel_timing import ONLINE_START, TimingBasis, timing_start, transport_archive_version, workday_minutes
from .filter_time import DATE_BASIS_FIELDS, DEFAULT_DATE_BASIS, REPORTING_WEEKS, DateBasis, time_bounds, week_filter
from .order_time import delivery_facts
from .tracking_archive import fill_local_times
from .waybill import key_sql, normalized as normalize_waybill

router = APIRouter(prefix="/api")

TRACK_STATUS_LABEL = "t.状态标签"
DELIVERY_WINDOWS = (2, 3, 5, 6, 7)


# ---------------------------------------------------------------- 公共

def delivery_window_counts(minutes, *, prefix="d", include_zero=False):
    minimum = ">=0" if include_zero else ">0"
    return ", ".join(
        f"count(*) FILTER (WHERE {minutes}{minimum} AND {minutes}<={day * 1440}) AS {prefix}{day}"
        for day in DELIVERY_WINDOWS
    )


def delivery_rates(counts, orders):
    return {str(day): round(100 * counts[day] / orders, 1) if orders else None for day in DELIVERY_WINDOWS}


def _cond(month="", wh="", pf="", cr="", cn="", status="", start="", end="", q="",
          alias="o", lastmile="", channel="", date_basis=DEFAULT_DATE_BASIS, week="", require_time=False,
          group_selection=(None, None)):
    """返回 (sql, args)，sql 形如 " AND ..."，可直接拼在 WHERE 1=1 之后。"""
    w, a = [], []

    def inl(column, value, parse=core.filter_values):
        vals = parse(value)
        if vals:
            w.append(f'{column} IN ({",".join("?" * len(vals))})')
            a.extend(vals)

    basis = date_basis or DEFAULT_DATE_BASIS
    timestamp_column = f"{alias}.{DATE_BASIS_FIELDS[basis]}"
    date_column = f"{alias}.创建日期" if basis == "created" else f"CAST({timestamp_column} AS DATE)"
    month_column = f"{alias}.创建月份" if basis == "created" else f"strftime({date_column}, '%Y-%m')"
    if require_time and basis != "created":
        w.append(f"{timestamp_column} IS NOT NULL")
    inl(month_column, month)
    week_sql, week_args = week_filter(date_column, core.filter_values(week))
    if week_sql:
        w.append(week_sql)
        a.extend(week_args)
    for column, value in (("仓库", wh), ("平台", pf), ("物流商", cr),
                          ("目的国", cn), ("状态", status), ("当前渠道", channel)):
        inl(f'{alias}."{column}"', value)
    inl(f"coalesce(nullif({alias}.C端承运商,''),'__empty__')", lastmile, core.last_mile_filter_values)
    group_sql, group_args = channel_groups.predicate(f"{alias}.当前渠道", group_selection)
    if group_sql:
        w.append(group_sql)
        a.extend(group_args)
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
    if core.columns(con, analytics_snapshot.SPLIT_TABLE):
        result = con.execute(f"""
            WITH kinds AS (
                SELECT g.kind FROM {analytics_snapshot.SPLIT_TABLE} g SEMI JOIN (
                    SELECT DISTINCT coalesce(trim(平台),'') AS platform,trim(订单号) AS order_no
                    FROM overview_timing WHERE nullif(trim(订单号),'') IS NOT NULL
                ) matched USING (platform,order_no)
            ) SELECT count(*) AS total,count(*) FILTER (WHERE kind<>'single') AS split,
                count(*) FILTER (WHERE kind='same') AS same,count(*) FILTER (WHERE kind='cross') AS cross,
                count(*) FILTER (WHERE kind='unknown') AS unknown,
                (SELECT count(*) FROM overview_timing WHERE nullif(trim(订单号),'') IS NULL) AS missingFulfillments
            FROM kinds
        """)
        return dict(zip((column[0] for column in result.description), result.fetchone()))
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


class OverviewChannelGroup(BaseModel):
    name: str = Field(max_length=80)
    channels: list[str] = Field(min_length=1, max_length=500)


class OverviewChannelGroups(BaseModel):
    revision: int = Field(ge=0)
    groups: list[OverviewChannelGroup] = Field(max_length=100)


@router.get("/overview/channel-groups")
def overview_channel_groups(request: Request):
    core.require_reader(request)
    with core.ops() as db:
        return channel_groups.load(db)


@router.put("/overview/channel-groups")
def update_overview_channel_groups(body: OverviewChannelGroups, request: Request):
    user = core.require_editor(request)
    groups, group_names, channel_names = [], set(), set()
    for item in body.groups:
        name = item.name.strip()
        channels = [value.strip() for value in item.channels]
        if (not name or name.casefold() in group_names
                or name.casefold() in (channel_groups.UNGROUPED, channel_groups.UNGROUPED_LABEL)):
            raise HTTPException(status_code=422, detail="组名必填，须唯一，且不能使用「未归组」")
        if any(not value or len(value) > 200 for value in channels):
            raise HTTPException(status_code=422, detail="请选择有效的当前渠道")
        if channel_names.intersection(channels) or len(set(channels)) != len(channels):
            raise HTTPException(status_code=422, detail="同一个当前渠道只能归入一组")
        group_names.add(name.casefold())
        channel_names.update(channels)
        groups.append({"name": name, "channels": channels})
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        if channel_groups.load(db)["revision"] != body.revision:
            raise HTTPException(status_code=409, detail="渠道分组已更新，请重新打开配置后再保存")
        result = {"revision": body.revision + 1, "groups": groups}
        rules.save_setting(db, "channel_group", "overview", result)
        core.audit(user["username"], "overview_channel_groups", "overview", {"groups": groups}, connection=db)
    return result


@router.get("/overview")
def overview(request: Request, month: str = "", wh: str = "", pf: str = "", cr: str = "",
             cn: str = "", start: str = "", end: str = "", lastmile: str = "", channel: str = "", week: str = "",
             date_basis: DateBasis = DEFAULT_DATE_BASIS, channel_group: str = ""):
    core.require_reader(request)
    with core.ops() as db:
        groups = channel_groups.load(db)["groups"]
    selection = channel_groups.filter_values(channel_group, groups)
    result = _overview(core.snapshot_key(), month, wh, pf, cr, cn, start, end, lastmile, channel, week,
                       date_basis or DEFAULT_DATE_BASIS, selection)
    distribution = channel_groups.distribution(
        groups, ((row["k"], row["n"]) for row in result["_currentChannels"]), result["kpi"]["orders"])
    return {**{key: value for key, value in result.items() if key != "_currentChannels"},
            "byChannelGroup": [{"k": row["t"], "n": row["n"], "group": row["v"]}
                               for row in distribution if row["n"]]}


@lru_cache(maxsize=32)
def _overview(_snapshot, month, wh, pf, cr, cn, start, end, lastmile, channel, week, date_basis, group_selection):
    def filters(basis, *, require_time=False):
        return _cond(month, wh, pf, cr, cn, start=start, end=end, lastmile=lastmile, channel=channel,
                     week=week, date_basis=basis, require_time=require_time, group_selection=group_selection)
    cond, args = filters(date_basis, require_time=True)

    con = core.duck()
    try:
        all_orders = core.order_facts(con)
        active_orders = core.active_facts(all_orders)
        order_facts = delivery_facts(con, active_orders)
        facts = analytics_snapshot.facts(con)
        tracking = track_lookup.source(con)
        con.execute(f"""
            CREATE TEMP TABLE overview_timing AS
            SELECT o.创建月份,o.仓内分钟,o.物流分钟,o.件数,o.仓库,o.物流商,o.目的国,o.目的国中文,
                   o.C端承运商,o.当前渠道,o.签出时间,o.支付时间,o.平台,o.订单号,o.{DELIVERED_BJ},o.{DELIVERY_SOURCE}
            FROM {facts} o WHERE 1=1{cond}
        """, args)
        k = con.execute(f"""
            SELECT count(*) n, coalesce(sum(件数),0) items, count({DELIVERED_BJ}) sign,
                   avg(仓内分钟) FILTER (WHERE 仓内分钟 > 0) hm,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0) tm,
                   count(*) FILTER (WHERE 物流分钟 > 0) sgn,
                   count(*) FILTER (WHERE {DELIVERY_SOURCE}='WMS') fallback_signed,
                   count(*) FILTER (WHERE {DELIVERY_SOURCE}='WMS' AND 物流分钟>0) fallback_samples,
                   {delivery_window_counts("物流分钟")}
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

        month_column = ("o.创建月份" if date_basis == "created"
                        else f"strftime(o.{DATE_BASIS_FIELDS[date_basis]}, '%Y-%m')")
        monthly = con.execute(f"""
            SELECT {month_column} m, count(*) n, count(o.{DELIVERED_BJ}) s,
                   avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0)/1440.0 d
            FROM overview_timing o GROUP BY 1 ORDER BY 1
        """).df().to_dict("records")
        daily = {}
        for basis, key in (("created", "daily"), ("shipped", "dailyShipped"), ("paid", "dailyPaid")):
            daily_cond, daily_args = filters(basis)
            field = DATE_BASIS_FIELDS[basis]
            rows = con.execute(f"""
                SELECT CAST(o.{field} AS DATE)::VARCHAR AS day, count(*) AS n
                FROM {active_orders} o WHERE o.{field} IS NOT NULL{daily_cond}
                GROUP BY 1 ORDER BY 1
            """, daily_args).fetchall()
            daily[key] = [{"day": day, "n": count} for day, count in rows]

        by_carrier = by("物流商", 20)
        by_wh = by("仓库", 20)
        by_country = by("目的国中文", 15)
        country_timing = con.execute("""
            SELECT 目的国 AS code,coalesce(max(nullif(目的国中文,'')),目的国) AS name,
                   count(*) AS orders,count(*) FILTER (WHERE 物流分钟>0) AS samples,
                   round(avg(物流分钟) FILTER (WHERE 物流分钟>0)/1440.0,2) AS days,
                   round(quantile_cont(物流分钟,0.90) FILTER (WHERE 物流分钟>0)/1440.0,2) AS p90
            FROM overview_timing WHERE nullif(trim(目的国),'') IS NOT NULL
            GROUP BY 1 ORDER BY orders DESC, name, code
        """).df().to_dict("records")
        status_cond, status_args = filters(date_basis)
        by_status = con.execute(f"""
            SELECT coalesce(o.状态, '未填写状态') k, count(*) n
            FROM {all_orders} o WHERE 1=1{status_cond} GROUP BY 1 ORDER BY n DESC,k
        """, status_args).df().to_dict("records")
        by_last_mile = by("C端承运商", 16)
        current_channels = con.execute("""SELECT 当前渠道 k, count(*) n FROM overview_timing
                                          GROUP BY 1 ORDER BY n DESC, k NULLS LAST""").df().to_dict("records")
        track_states = con.execute(f"""
            SELECT {TRACK_STATUS_LABEL} k, count(*) n
            FROM {order_facts} o
            LEFT JOIN {tracking} t ON t.履约单号=o.履约单号
            WHERE 1=1{cond}
            GROUP BY 1 ORDER BY 2 DESC
        """, args).df().to_dict("records")
        asof = con.execute(f"SELECT max(创建日期) FROM {active_orders}").fetchone()[0]
    finally:
        con.close()

    n = k[0] or 0
    sgn = k[5] or 0
    window_counts = dict(zip(DELIVERY_WINDOWS, k[8:], strict=True))
    sample_rates = delivery_rates(window_counts, sgn)

    return core.clean({
        "asof": str(asof),
        "kpi": {
            "orders": n, "items": int(k[1] or 0),
            "signRate": round(100.0 * (k[2] or 0) / n, 1) if n else None,
            "inboundHours": round(k[3] / 60.0, 2) if k[3] else None,
            "transitDays": round(k[4] / 1440.0, 2) if k[4] else None,
            "transitSamples": sgn,
            "deliveryRates": delivery_rates(window_counts, n),
            # Existing API fields continue to mean rates among valid timing samples.
            **{f"rate{day}": sample_rates[str(day)] for day in DELIVERY_WINDOWS if day != 2},
            "signed": int(k[2] or 0),
            "fallbackSigned": k[6], "fallbackTransitSamples": k[7],
        },
        "monthly": monthly, "byCarrier": by_carrier, "byWarehouse": by_wh,
        "warehouseShipping": warehouse_shipping, "orderSplit": order_split,
        **daily,
        "byCountry": by_country, "countryTiming": country_timing, "byStatus": by_status,
        "byLastMile": by_last_mile, "byCurrentChannel": current_channels[:10], "_currentChannels": current_channels,
        "byTrackingStatus": track_states, "dateBasis": date_basis,
    })


# ---------------------------------------------------------------- 区域分析

@router.get("/regional-analysis")
@router.get("/us-analysis")
def usa(request: Request, month: str = "", wh: str = "", pf: str = "", cr: str = "",
        start: str = "", end: str = "", lastmile: str = "", channel: str = "", week: str = "",
        date_basis: DateBasis = DEFAULT_DATE_BASIS, channel_group: str = "",
        basis: us_analysis.TransitBasis = "paid", dimension: us_analysis.ChannelDimension = "lastmile",
        market: us_analysis.Market = "us", cn: str = "", detail: Literal["map", "segments"] = "map",
        level: us_analysis.PlaceLevel = "all", place: str = "", focus: str = ""):
    core.require_user(request)
    groups = []
    if channel_group or dimension == "group":
        with core.ops() as db:
            groups = channel_groups.load(db)["groups"]
    date_basis = date_basis or DEFAULT_DATE_BASIS
    cond, args = _cond(month, wh, pf, cr, cn=cn if market == "other" else "",
                       start=start, end=end, lastmile=lastmile, channel=channel, week=week,
                       date_basis=date_basis, require_time=True,
                       group_selection=channel_groups.filter_values(channel_group, groups))
    settings = json.dumps(groups, ensure_ascii=False, sort_keys=True) if dimension == "group" else ""
    snapshot = core.snapshot_key()
    if detail == "segments":
        return us_analysis.segments(snapshot, market, date_basis, dimension, cond, tuple(args), settings,
                                    level, place, focus, transport_archive_version())
    archive_version = transport_archive_version() if basis in ("ship_pickup", "pickup_delivered") else None
    return us_analysis.report(snapshot, date_basis, basis, dimension, cond, tuple(args), settings, market, archive_version)


# ---------------------------------------------------------------- 轨迹监控

def _track_selection(channel_group, resolved_only):
    if not (channel_group or resolved_only):
        return (None, None), None
    with core.ops() as db:
        selection = channel_groups.filter_values(channel_group, channel_groups.load(db)["groups"]) if channel_group else (None, None)
        resolved = alert_history.resolved_for_orders(db, manual_only=True) if resolved_only else None
    return selection, resolved


def _track_conditions(*, q, month, wh, pf, cr, cn, tstat, rule, signed, start, end,
                      lastmile, sub, coverage, channel, week, date_basis, selection):
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, q=q, lastmile=lastmile, channel=channel,
                       date_basis=date_basis, week=week, group_selection=selection)

    for column, value in (("t.状态", tstat), ("t.子状态", sub), ("a.主预警规则", rule)):
        vals = core.filter_values(value)
        if vals:
            cond += f' AND {column} IN ({",".join("?" * len(vals))})'
            args.extend(vals)
    if coverage == "canceled_unstored":
        cond += " AND FALSE"
    if coverage == "tracked":
        cond += " AND t.覆盖 IN ('tracked','queried_empty')"
    elif coverage == "unknown":
        cond += " AND t.单号 IS NULL AND NOT t.未注册 AND t.回补状态 IS NULL"
    elif coverage == "carrier_conflict":
        cond += " AND t.核对结果='carrier_conflict'"
    elif coverage == "detailed":
        cond += " AND t.逐条事件数>0"
    elif coverage in track_lookup.LABELS:
        cond += " AND t.覆盖=?"; args.append(coverage)
    if signed in ("0", "1"):
        cond += " AND o.已签收 = ?"; args.append(int(signed))
    return cond, args


def _register_live_alerts(con, condition, arguments):
    with core.ops() as ops:
        summaries = ops.execute(f"""
            WITH ranked AS (
                SELECT order_no, rule_cn, rule, level, count(*) OVER by_order AS total,
                       row_number() OVER (by_order ORDER BY is_main DESC,
                          CASE level WHEN '紧急' THEN 0 WHEN '高' THEN 1 ELSE 2 END, rule) AS choice
                FROM ({alert_history.current_rows()})
                WHERE {condition} AND state IN {alert_history.PENDING_STATES}
                WINDOW by_order AS (PARTITION BY order_no)
            )
            SELECT order_no,rule_cn,rule,level,total FROM ranked WHERE choice=1
        """, arguments).fetchall()
    con.register("live_alerts", pd.DataFrame.from_records(
        summaries, columns=["履约单号", "主预警名称", "主预警规则", "主预警级别", "预警数"]
    ).astype({"履约单号": "string", "主预警名称": "string", "主预警规则": "string",
              "主预警级别": "string", "预警数": "int64"}))


class TrackAnalysisScope(BaseModel):
    q: str = Field("", max_length=128)
    month: str = Field("", max_length=4096)
    week: str = Field("", max_length=4096)
    wh: str = Field("", max_length=4096)
    pf: str = Field("", max_length=4096)
    cr: str = Field("", max_length=4096)
    cn: str = Field("", max_length=4096)
    tstat: str = Field("", max_length=128)
    rule: str = Field("", max_length=128)
    signed: str = Field("", max_length=4)
    start: str = Field("", max_length=32)
    end: str = Field("", max_length=32)
    lastmile: str = Field("", max_length=4096)
    sub: str = Field("", max_length=128)
    coverage: str = Field("", max_length=64)
    channel: str = Field("", max_length=8192)
    date_basis: DateBasis = DEFAULT_DATE_BASIS
    channel_group: str = Field("", max_length=4096)
    resolved_only: bool = False


@router.post("/track-analysis", status_code=200, response_class=ops_assistant.PrivateJSON)
def analyze_track(body: TrackAnalysisScope, request: Request, interpret: bool = False):
    user = core.require_reader(request)
    if user["preview"]:
        raise HTTPException(403, "当前预览模式只能查询单笔轨迹")
    ai_available = ops_assistant.can_use(user) and ops_assistant.configured()["ai"]
    if interpret:
        ops_assistant.authorize(request, "ai")
    selection, resolved = _track_selection(body.channel_group, body.resolved_only)
    cond, args = _track_conditions(**body.model_dump(exclude={"channel_group", "resolved_only"}), selection=selection)
    if not track_analysis.RUNNING.acquire(blocking=False):
        raise HTTPException(429, "已有轨迹分析正在计算，请稍后再试")
    try:
        with core.duck() as con:
            facts = delivery_facts(con, core.active_facts(core.order_facts(con)))
            if resolved is not None:
                con.register("analysis_resolved", pd.DataFrame({"id": pd.Series(resolved.keys(), dtype="string")}))
                cond += " AND o.履约单号 IN (SELECT id FROM analysis_resolved)"
            alert_join = ""
            if body.rule:
                _register_live_alerts(con, rules.visible_alerts(), [])
                alert_join = "LEFT JOIN live_alerts a ON a.履约单号=o.履约单号"
            con.execute(f"""
                CREATE TEMP TABLE analysis_orders AS
                SELECT o.履约单号 AS id,o.单号归一 AS num,
                    coalesce(t.carrier,t.回补运输商代码) AS carrier,o.签出时间 AS shipped,
                    coalesce(o.C端承运商,o.物流商,'未识别') AS carrier_name,
                    coalesce(o.仓库,'—') AS warehouse,coalesce(o.当前渠道,'—') AS channel,
                    t.状态 AS official,t.子状态 AS latest_sub,t.查询时间 AS checked,
                    coalesce(o.已签收=1,false) AS wms_delivered
                FROM {facts} o LEFT JOIN {track_lookup.source(con)} t ON t.履约单号=o.履约单号
                {alert_join} WHERE 1=1{cond}
            """, args)
            result = track_analysis.report(con)
    finally:
        track_analysis.RUNNING.release()
    if interpret and result["summary"]["tracked"]:
        result["interpretation"] = ops_assistant.trajectory_summary(result)
    return {**result, "aiAvailable": bool(ai_available)}


@router.get("/track")
def track(request: Request,
          page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
          q: str = "", month: str = "", wh: str = "", pf: str = "", cr: str = "",
          cn: str = "", tstat: str = "", rule: str = "", signed: str = "",
          sort: str = "", order: str = "desc",
          start: str = "", end: str = "", lastmile: str = "",
          sub: str = "", coverage: str = "", channel: str = "", week: str = "",
          date_basis: DateBasis = DEFAULT_DATE_BASIS, channel_group: str = "", resolved_only: bool = False,
          part: Literal["all", "rows", "summary"] = "all"):
    if core.require_reader(request)["preview"]:
        return public_preview.search("" if resolved_only else q, page, size)
    selection, resolved = _track_selection(channel_group, resolved_only)
    if resolved == {}:
        return {"total": 0, "page": page, "size": size, "rows": [], "statusSummary": [], "ruleSummary": []}
    cond, args = _track_conditions(q=q, month=month, wh=wh, pf=pf, cr=cr, cn=cn, tstat=tstat,
                                   rule=rule, signed=signed, start=start, end=end, lastmile=lastmile,
                                   sub=sub, coverage=coverage, channel=channel, week=week,
                                   date_basis=date_basis, selection=selection)

    sort_cols = {"停滞天数": "t.停滞天数", "签出时间": "o.签出时间", "创建时间": "o.创建时间",
                 "物流分钟": "o.物流分钟", "履约单号": "o.履约单号", "逐条事件数": "逐条事件数"}
    if resolved_only:
        sort_cols["解决时间"] = "o.解决时间"
    sc = sort_cols.get(sort, "o.解决时间" if resolved_only else "t.停滞天数")
    direction = "ASC" if order.lower() == "asc" else "DESC"
    off = (page - 1) * size

    con = core.duck()
    try:
        facts = delivery_facts(con, core.active_facts(core.order_facts(con)))
        if resolved_only:
            con.register("resolved_alerts", pd.DataFrame.from_records(
                [(order_no, row["handle_at"]) for order_no, row in resolved.items()],
                columns=["履约单号", "解决时间"],
            ).astype("string"))
            facts = f"""(SELECT f.*, r.解决时间 FROM {facts} f
                         INNER JOIN resolved_alerts r USING (履约单号))"""
        order_only = not any((tstat, sub, rule, coverage))
        tracking = track_lookup.source(con)
        order_sort = sc in ("o.签出时间", "o.创建时间", "o.履约单号", "o.解决时间")
        fast_page = part == "rows" and not rule and (order_sort or sc in ("t.停滞天数", "逐条事件数"))
        search_total = None
        if fast_page:
            # Select the visible orders before joining carrier status and alerts.
            page_cond, page_args = cond, args
            if not order_only or not order_sort:
                sort_value = "t.逐条事件数" if sc == "逐条事件数" else sc
                con.execute(f"""
                    CREATE TEMP TABLE matching_track_orders AS
                    SELECT o.履约单号,{sort_value} AS sort_value FROM {facts} o
                    JOIN {tracking} t ON t.履约单号=o.履约单号 WHERE 1=1{cond}
                """, args)
                search_total = con.execute("SELECT count(*) FROM matching_track_orders").fetchone()[0]
                if off >= search_total:
                    return {"total": search_total, "page": page, "size": size, "rows": [],
                            "statusSummary": [], "ruleSummary": []}
                con.execute(f"""
                    CREATE TEMP TABLE selected_track_orders AS
                    SELECT o.* FROM {facts} o SEMI JOIN (
                        SELECT 履约单号 FROM matching_track_orders
                        ORDER BY sort_value {direction} NULLS LAST,履约单号 LIMIT ? OFFSET ?
                    ) selected USING (履约单号)
                """, [size, off])
            elif q.strip():
                con.execute(f"""
                    CREATE TEMP TABLE matching_track_orders AS
                    SELECT o.履约单号 FROM {facts} o WHERE 1=1{cond}
                """, args)
                search_total = con.execute("SELECT count(*) FROM matching_track_orders").fetchone()[0]
                if off >= search_total:
                    return {"total": search_total, "page": page, "size": size, "rows": [],
                            "statusSummary": [], "ruleSummary": []}
                page_cond, page_args = " AND o.履约单号 IN (SELECT 履约单号 FROM matching_track_orders)", []
            if order_only and order_sort:
                con.execute(f"""
                    CREATE TEMP TABLE selected_track_orders AS
                    SELECT o.* FROM {facts} o WHERE 1=1{page_cond}
                    ORDER BY {sc} {direction} NULLS LAST, o.履约单号
                    LIMIT ? OFFSET ?
                """, [*page_args, size, off])
        alert_where, alert_args = rules.visible_alerts(), []
        if fast_page:
            alert_args = [row[0] for row in con.execute("SELECT 履约单号 FROM selected_track_orders").fetchall()]
            alert_where += f' AND order_no IN ({",".join("?" * len(alert_args))})' if alert_args else " AND FALSE"
        _register_live_alerts(con, alert_where, alert_args)
        joins = (f"LEFT JOIN {tracking} t ON t.履约单号 = o.履约单号 "
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
        resolution_column = "o.解决时间," if sc == "o.解决时间" else ""
        page_query = f"""
            SELECT o.履约单号, o.订单号, o.快递单号, o.单号归一, o.平台, o.仓库, o.物流商, o.C端承运商, o.当前渠道,
                   o.目的国中文, o.状态, o.支付时间, o.创建时间, o.签出时间, o.签收时间,
                   {timing_columns} {resolution_column}
                   t.签收时间 AS tracked_delivery,t.签收当地时间 AS tracked_delivery_local,
                   o.签收北京时间, o.签收时区,
                   o.物流分钟, o.已签收,
                   t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
                   t.最新事件时间, t.最新事件当地时间, t.最新描述, t.地点, t.停滞天数,
                   t.回补运输商代码,
                   t.carrier AS 运输商代码, t.查询时间, t.回补状态,
                   t.逐条事件数,t.状态标签,t.核对结果,
                   a.主预警名称, a.主预警规则, a.主预警级别, a.预警数,
                   t.未注册
            FROM {page_facts} o {joined}
            ORDER BY {sc} {direction} NULLS LAST, o.履约单号
            LIMIT ? OFFSET ?
        """
        measured = "page_rows" if timed_sort else reporting_facts(con, "page_rows", page_only=True)
        excluded = [DELIVERED_UTC, DELIVERED_BJ, "tracked_delivery", "tracked_delivery_local"]
        sort_field = sc.split(".")[-1]
        page_query = f"""
            WITH page_rows AS MATERIALIZED ({page_query})
            SELECT p.* EXCLUDE ({', '.join(excluded)}),
                   p.{DELIVERED_UTC} AS 承运商签收时间,
                   CASE WHEN p.tracked_delivery=p.{DELIVERED_UTC} THEN p.tracked_delivery_local END AS 签收当地时间,
                   {transport_minutes('p', "p.轨迹状态 IS NOT DISTINCT FROM 'Delivered'")} AS 运输分钟
            FROM {measured} p ORDER BY p."{sort_field}" {direction} NULLS LAST, p.履约单号
        """
        rows = con.execute(page_query, args + [size, off]).df().to_dict("records")
        fill_local_times(con, rows)
        if resolved is None:
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

def _channel_duration(basis, online_start):
    start = {"paid": "o.支付时间", "shipped": "o.签出时间", "online": online_start}[basis]
    return start, "o.物流分钟" if basis == "shipped" else elapsed_minutes(start, f"o.{DELIVERED_BJ}")


@router.get("/channel-trends")
def channel_trends(request: Request, key: str = Query(..., min_length=1, max_length=240),
                   month: str = "", wh: str = "", pf: str = "", cr: str = "", cn: str = "",
                   start: str = "", end: str = "", lastmile: str = "", channel: str = "",
                   week: str = "", basis: TimingBasis = "shipped", date_basis: DateBasis = DEFAULT_DATE_BASIS,
                   channel_group: str = "", dimension: Literal["lastmile", "group"] = "lastmile"):
    core.require_reader(request)
    date_basis = date_basis or DEFAULT_DATE_BASIS
    with core.ops() as ops:
        groups = channel_groups.load(ops)["groups"] if channel_group or dimension == "group" else []
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, lastmile=lastmile, channel=channel,
                       week=week, date_basis=date_basis, require_time=True,
                       group_selection=channel_groups.filter_values(channel_group, groups))
    if dimension == "group":
        selected, values = channel_groups.predicate("o.当前渠道",
                                                    channel_groups.filter_values(json.dumps([key]), groups))
        if selected:
            cond += f" AND {selected}"
            args.extend(values)
    else:
        cond += " AND coalesce(nullif(o.C端承运商,''),'__empty__')=?"
        args.append(key)
    months = tuple(core.filter_values(month))
    weeks = tuple(REPORTING_WEEKS[value] for value in core.filter_values(week))
    return _channel_trends(core.snapshot_key(), transport_archive_version() if basis == "online" else None,
                           date_basis, basis, cond, tuple(args), time_bounds(start, end), months, weeks)


def _period_is_partial(period_start, period_end, first, latest, bounds, months, weeks):
    lower, upper = (datetime.combine(bound, time.min) if bound and not isinstance(bound, datetime)
                    else bound for bound in bounds)
    start = datetime.combine(period_start, time.min)
    end = datetime.combine(period_end, time.min)
    if period_start < first.date() or latest < end or (lower and lower > start) or (upper and upper < end):
        return True
    for index in range((period_end - period_start).days):
        day = period_start + timedelta(days=index)
        if months and day.strftime("%Y-%m") not in months:
            return True
        if weeks and not any(left <= day < right for left, right in weeks):
            return True
    return False


@lru_cache(maxsize=64)
def _channel_trends(_snapshot, archive, date_basis, basis, cond, args, bounds, months, weeks):
    field = DATE_BASIS_FIELDS[date_basis]
    with core.duck() as con:
        raw_facts = core.active_facts(core.order_facts(con))
        first, latest = con.execute(f"SELECT min({field}),max({field}) FROM {raw_facts}").fetchone()
        facts = analytics_snapshot.facts(con)
        join, online_start = analytics_snapshot.scan_source(con, archive) if basis == "online" else ("", ONLINE_START)
        report_start, minutes = _channel_duration(basis, online_start)
        cursor = con.execute(f"""
            WITH scoped AS (
                SELECT date_trunc('month',o.{field})::DATE AS month_start,
                       time_bucket(INTERVAL '7 days',o.{field}::DATE,DATE '2000-01-02') AS week_start,
                       {minutes} AS duration_min,{report_start} AS report_start,o.{DELIVERED_BJ} AS delivered
                FROM {facts} o {join} WHERE o.{field} IS NOT NULL{cond}
            ), measured AS (
                SELECT *,CASE WHEN duration_min>0 THEN {workday_minutes("report_start", "delivered")} END AS workday_min
                FROM scoped
            )
            SELECT grouping(week_start) AS by_month,coalesce(month_start,week_start) AS period,
                   count(*) AS orders,count(*) FILTER (WHERE duration_min>0) AS samples,
                   round(avg(duration_min) FILTER (WHERE duration_min>0)/1440.0,2) AS avg,
                   round(quantile_cont(duration_min,0.90) FILTER (WHERE duration_min>0)/1440.0,2) AS p90,
                   count(*) FILTER (WHERE duration_min>0 AND duration_min<=8640) AS within6,
                   count(*) FILTER (WHERE workday_min>=0 AND workday_min<=8640) AS workdayWithin6
            FROM measured GROUP BY GROUPING SETS ((month_start),(week_start)) ORDER BY 1,2
        """, list(args))
        names = [column[0] for column in cursor.description]
        records = [dict(zip(names, row)) for row in cursor.fetchall()]
    trend = {"month": [], "week": [], "dateBasis": date_basis, "basis": basis}
    for row in records:
        period = row.pop("period")
        by_month = row.pop("by_month")
        if by_month:
            end = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            end = period + timedelta(days=7)
        partial = _period_is_partial(period, end, first, latest, bounds, months, weeks)
        row["rate6"] = round(100.0 * row.pop("within6") / row["orders"], 1)
        row["workdayRate6"] = round(100.0 * row.pop("workdayWithin6") / row["orders"], 1)
        trend["month" if by_month else "week"].append({**row, "start": period, "end": end, "partial": partial})
    return core.clean(trend)


@router.get("/channels")
def channels(request: Request, month: str = "", wh: str = "", pf: str = "",
             cn: str = "", min_n: int = Query(100, ge=1), cr: str = "", lastmile: str = "",
             start: str = "", end: str = "", channel: str = "", week: str = "",
             basis: TimingBasis = "shipped", date_basis: DateBasis = DEFAULT_DATE_BASIS, channel_group: str = "",
             dimension: Literal["lastmile", "group"] = "lastmile"):
    core.require_reader(request)
    if basis == "created":
        basis = "paid"  # Value used by earlier clients for 下单到签收.
    with core.ops() as ops:
        _, saved = rules.load_settings(ops)
        groups = channel_groups.load(ops)["groups"] if channel_group or dimension == "group" else []
        selection = channel_groups.filter_values(channel_group, groups)
    archive = transport_archive_version() if basis == "online" or any(
        item.get("basis") == "online" for item in saved.values()
    ) else None
    date_basis = date_basis or DEFAULT_DATE_BASIS
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, lastmile=lastmile, channel=channel, week=week,
                       date_basis=date_basis, require_time=True, group_selection=selection)
    grouping = json.dumps(groups, ensure_ascii=False, sort_keys=True) if dimension == "group" else ""
    report = _channel_report(core.snapshot_key(), archive, json.dumps(saved, sort_keys=True), basis, cond, tuple(args),
                             date_basis, dimension, grouping)
    result = {**report, "dateBasis": date_basis, "rows": [row for row in report["rows"] if row["orders"] >= min_n]}
    if dimension == "group":
        selected = core.filter_values(channel_group)
        counts = {row["key"]: row["orders"] for row in report["rows"]}
        result["memberGroups"] = sorted(
            ({"key": group["name"], "orders": counts.get(group["name"], 0)}
             for group in groups if not selected or group["name"] in selected),
            key=lambda group: (-group["orders"], group["key"]),
        )
    return result


@router.get("/channel-members")
def channel_members(request: Request, key: str = Query(..., min_length=1, max_length=240),
                    month: str = "", wh: str = "", pf: str = "", cr: str = "", cn: str = "",
                    start: str = "", end: str = "", lastmile: str = "", channel: str = "", week: str = "",
                    basis: TimingBasis = "shipped", date_basis: DateBasis = DEFAULT_DATE_BASIS,
                    channel_group: str = ""):
    core.require_reader(request)
    with core.ops() as ops:
        groups = channel_groups.load(ops)["groups"]
    group = next((group for group in groups if group["name"] == key), None)
    if group is None:
        raise HTTPException(404, "未找到该自定义渠道")
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end, lastmile=lastmile, channel=channel,
                       week=week, date_basis=date_basis, require_time=True,
                       group_selection=channel_groups.filter_values(channel_group, groups))
    members = tuple(group["channels"])
    result = _channel_members(core.snapshot_key(), transport_archive_version() if basis == "online" else None,
                              basis, members, cond, tuple(args))
    return {**result, "group": key}


@lru_cache(maxsize=48)
def _channel_members(_snapshot, archive, basis, members, cond, args):
    with core.duck() as con:
        facts = analytics_snapshot.facts(con)
        join, online_start = analytics_snapshot.scan_source(con, archive) if basis == "online" else ("", ONLINE_START)
        report_start, minutes = _channel_duration(basis, online_start)
        cursor = con.execute(f"""
            WITH scoped AS (
                SELECT trim(o.当前渠道) AS channel,coalesce(nullif(o.C端承运商,''),'__empty__') AS lastmile,
                       {minutes} AS duration_min,
                       {report_start} AS report_start,o.{DELIVERED_BJ} AS delivered
                FROM {facts} o {join}
                WHERE trim(o.当前渠道) IN (SELECT unnest(?::VARCHAR[])){cond}
            ), measured AS (
                SELECT *,CASE WHEN duration_min>0 THEN {workday_minutes("report_start", "delivered")} END AS workday_min
                FROM scoped
            )
            SELECT channel,lastmile,count(*) AS orders,count(*) FILTER(WHERE duration_min>0) AS samples,
                   round(avg(duration_min) FILTER(WHERE duration_min>0)/1440.0,2) AS "avgDays",
                   round(quantile_cont(duration_min,0.90) FILTER(WHERE duration_min>0)/1440.0,2) AS "p90Days",
                   {delivery_window_counts("duration_min")},
                   {delivery_window_counts("workday_min", prefix="w", include_zero=True)}
            FROM measured GROUP BY GROUPING SETS ((channel),(lastmile),(channel,lastmile))
        """, [list(members), *args])
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    empty_rates = delivery_rates({}, 0)
    by_name = {
        name: {"channel": name, "orders": 0, "samples": 0, "avgDays": None, "p90Days": None,
               "deliveryRates": empty_rates, "workdayDeliveryRates": empty_rates}
        for name in members
    }
    lastmile_rows = []
    by_member = {name: [] for name in members}
    for row in rows:
        channel = row.pop("channel")
        lastmile = row.pop("lastmile")
        row["deliveryRates"] = delivery_rates({day: row.pop(f"d{day}") for day in DELIVERY_WINDOWS}, row["orders"])
        row["workdayDeliveryRates"] = delivery_rates({day: row.pop(f"w{day}") for day in DELIVERY_WINDOWS}, row["orders"])
        if lastmile is None:
            by_name[channel].update(row)
        else:
            row["key"] = lastmile
            row["channel"] = "未填写 C 端渠道" if lastmile == "__empty__" else lastmile
            (lastmile_rows if channel is None else by_member[channel]).append(row)
    ordered = sorted(by_name.values(), key=lambda row: (-row["orders"], row["channel"]))
    for row in ordered:
        row["lastmileRows"] = sorted(by_member[row["channel"]], key=lambda member: (-member["orders"], member["channel"]))
    lastmile_rows.sort(key=lambda row: (-row["orders"], row["channel"]))
    return core.clean({"orders": sum(row["orders"] for row in ordered), "rows": ordered, "lastmileRows": lastmile_rows})


@lru_cache(maxsize=32)
def _channel_report(_snapshot, _archive, saved, basis, cond, args, date_basis, dimension="lastmile", grouping=""):
    con = core.duck()
    try:
        raw_facts = core.active_facts(core.order_facts(con))
        facts = analytics_snapshot.facts(con)
        asof = con.execute(f"SELECT max(创建日期) FROM {raw_facts}").fetchone()[0]
        field = DATE_BASIS_FIELDS[date_basis]
        date_asof = con.execute(f"SELECT CAST(max({field}) AS DATE) FROM {raw_facts}").fetchone()[0]
        slas = rules.current_channel_slas(con, raw_facts, json.loads(saved))
        sla_sql, sla_values = rules.sla_table(slas)
        sla_scans = any(config["basis"] == "online" for config in slas.values())
        scan_join, online_start = analytics_snapshot.scan_source(con, _archive) if basis == "online" or sla_scans else ("", ONLINE_START)
        report_start, minutes = _channel_duration(basis, online_start)
        sla_start = timing_start("tgt.basis", sla_scans, online_start)
        active = f"o.已签收=0 AND o.{DELIVERED_UTC} IS NULL AND (o.状态 IS DISTINCT FROM '已完成' OR o.签收时间 IS NOT NULL) AND o.签出时间 IS NOT NULL"
        group_join = ""
        key = "coalesce(nullif(o.C端承运商,''),'__empty__')"
        if dimension == "group":
            group_rows = [(channel,group["name"]) for group in json.loads(grouping) for channel in group["channels"]]
            con.register("comparison_groups", pd.DataFrame(group_rows, columns=["channel","label"]).astype("string"))
            group_join = "LEFT JOIN comparison_groups g ON g.channel=trim(o.当前渠道)"
            key = f"coalesce(g.label,'{channel_groups.UNGROUPED}')"
        scoped = f"""SELECT {key} AS key,
                            coalesce(o.目的国中文,o.目的国) AS ctry,
                            o.{DELIVERED_BJ} AS delivered, o.{DELIVERY_SOURCE} AS delivery_source,
                            {minutes} AS duration_min, {report_start} AS report_start, {sla_start} AS sla_start,
                            coalesce(tgt.days,{rules.DEFAULT_SLA_DAYS}) AS sla_days,
                            {active} AS in_transit
                     FROM {facts} o LEFT JOIN tgt ON tgt.ch=o.当前渠道
                     {group_join} {scan_join} WHERE 1=1{cond}"""
        usable = "o.duration_min>0"
        sla_minutes = elapsed_minutes("o.sla_start", "o.delivered")
        sla_usable = f"{sla_minutes}>0"
        grouped = con.execute(f"""
            WITH tgt(ch, days, basis) AS ({sla_sql}), scoped AS ({scoped}), measured AS (
                SELECT *, CASE WHEN duration_min>0 THEN {workday_minutes("report_start", "delivered")} END AS workday_min
                FROM scoped
            )
            SELECT grouping(o.ctry) AS channel_total, o.key, o.ctry, count(*) AS orders,
                   count(o.delivered) AS signed,
                   count(*) FILTER (WHERE {usable}) AS samples,
                   count(*) FILTER (WHERE {usable} AND o.delivery_source='WMS') AS fallbackSamples,
                   round(avg(o.duration_min) FILTER (WHERE {usable}) / 1440.0, 2) AS avgDays,
                   round(quantile_cont(o.duration_min, 0.9) FILTER (WHERE {usable}) / 1440.0, 2) AS p90Days,
                   {delivery_window_counts("o.duration_min")},
                   {delivery_window_counts("o.workday_min", prefix="w", include_zero=True)},
                   count(*) FILTER (WHERE {sla_usable}) AS sla_samples,
                   count(*) FILTER (WHERE {sla_usable} AND {sla_minutes}<=o.sla_days*1440) AS within_sla,
                   count(*) FILTER (WHERE o.in_transit) AS inTransit,
                   count(*) FILTER (WHERE o.in_transit AND o.sla_start IS NOT NULL) AS slaInTransit,
                   count(*) FILTER (WHERE o.in_transit AND
                      date_diff('second',o.sla_start,?::TIMESTAMP)>o.sla_days*86400) AS overdue
            FROM measured o
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
        window_counts = {day: row.pop(f"d{day}") for day in DELIVERY_WINDOWS}
        workday_counts = {day: row.pop(f"w{day}") for day in DELIVERY_WINDOWS}
        row["deliveryRates"] = delivery_rates(window_counts, row["orders"])
        row["workdayDeliveryRates"] = delivery_rates(workday_counts, row["orders"])
        name = row["key"]
        if dimension == "group" and name == channel_groups.UNGROUPED:
            name = channel_groups.UNGROUPED_LABEL
        elif dimension == "lastmile" and name == "__empty__":
            name = "未填写 C 端渠道"
        if not row.pop("channel_total"):
            if len(by_country) < 40:
                by_country.append({"key": row["key"], "ch": name, "ctry": row["ctry"], "n": row["orders"],
                                   "avgDays": row["avgDays"], "samples": row["samples"],
                                   "deliveryRates": row["deliveryRates"],
                                   "workdayDeliveryRates": row["workdayDeliveryRates"],
                                   "rate6": pct(window_counts[6], row["samples"])})
            continue
        row.pop("ctry")
        row["channel"] = name
        for day in (3, 5, 6, 7):
            row[f"rate{day}"] = pct(window_counts[day], row["samples"])
        row["slaRate"] = pct(row.pop("within_sla"), row["sla_samples"])
        row["overRate"] = pct(row["overdue"], row["slaInTransit"])
        records.append(row)
    return core.clean({"asof": str(asof), "dateAsof": date_asof, "basis": basis, "dimension": dimension,
                       "rows": records, "byCountry": by_country})


# ---------------------------------------------------------------- 规则与 SLA

@router.get("/rules")
def rules_api(request: Request):
    core.require_user(request)
    with core.ops() as ops:
        rule_overrides, sla_overrides = rules.load_settings(ops)
        membership = {channel: group["name"] for group in channel_groups.load(ops)["groups"]
                      for channel in group["channels"]}
        hits = dict(ops.execute(
            f"SELECT rule,count(*) FROM alerts WHERE is_main AND {rules.visible_alerts()} GROUP BY rule"
        ).fetchall())
    con = core.duck()
    try:
        raw_facts = core.active_facts(core.order_facts(con))
        facts = analytics_snapshot.facts(con)
        asof = con.execute(f"SELECT max(创建日期) FROM {raw_facts}").fetchone()[0]
        slas = rules.current_channel_slas(con, raw_facts, sla_overrides)
        sla_sql, sla_values = rules.sla_table(slas)
        have_online = any(config["basis"] == "online" for config in slas.values())
        scan_join, online_start = analytics_snapshot.scan_source(con, transport_archive_version()) if have_online else ("", ONLINE_START)
        start_at = timing_start("tgt.basis", have_online, online_start)
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
                   customized=row["ch"] in sla_overrides,
                   channelGroup=membership.get(row["ch"].strip(), channel_groups.UNGROUPED))
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
