"""REST API：订单查询、预警处理、统计。"""
from __future__ import annotations

from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from io import StringIO
from math import isfinite
import re
from typing import Any, Literal, Optional
from unicodedata import normalize
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from track17 import OFFICIAL_STATUS

from . import alert_history, channel_groups, cloud_login, core, ops_auth, ops_assistant, public_preview
from .carrier_delivery import DELIVERED_BJ, DELIVERED_UTC, reporting_facts, transport_minutes
from . import track_lookup
from .filter_time import DATE_BASIS_FIELDS, DEFAULT_DATE_BASIS, REPORTING_WEEKS, DateBasis, time_bounds, week_filter
from .order_time import delivery_facts
from . import tracking_archive, tracking_freshness
from .rules import RULES as BUSINESS_RULES, visible_alerts
from .tracking_clock import utc_key
from .waybill import normalized as normalize_waybill

router = APIRouter(prefix="/api")
ALERT_CHANNEL_VALUE = "coalesce(nullif(a.current_channel,''),'__empty__')"


def _revision():
    try:
        stamps = [core.snapshot_key()]
    except FileNotFoundError:
        return None
    for path in (core.OPS_DB, core.OPS_DB.with_name(core.OPS_DB.name + "-wal")):
        try:
            stat = path.stat()
            stamps.append((stat.st_size,stat.st_mtime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return sha256(repr(stamps).encode()).hexdigest()


@router.get("/data-revision")
def data_revision(request: Request):
    core.require_reader(request)
    return {"revision": _revision()}

# ---------------------------------------------------------------- 认证

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    from init_ops import verify_pw

    ops_auth.require_same_origin(request)
    if not ops_auth.local_login_enabled():
        raise HTTPException(403, "请通过智能中枢登录")
    with core.ops() as con:
        row = con.execute("SELECT username,password_hash,active,display_name,role FROM users WHERE username=?",
                          (body.username,)).fetchone()
    if (not row or not row["active"] or not core.allowed_online_account(row["username"])
            or not verify_pw(body.password, row["password_hash"])):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    sid, exp = core.create_session(body.username,
                                   ip=request.client.host if request.client else "",
                                   ua=request.headers.get("user-agent", ""))
    core.audit(body.username, "login", body.username)
    core.drop_session(request.cookies.get(core.SESSION_COOKIE, ""))
    core.set_session_cookie(request, response, sid)
    role = row["role"]
    return {"ok": True, "user": {
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": role,
        "can_edit": core.ROLES.get(role, {}).get("can_edit", False),
        "role_label": core.ROLES.get(role, {}).get("label", role),
    }}


@router.post("/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get(core.SESSION_COOKIE)
    core.drop_session(sid)
    response.delete_cookie(core.SESSION_COOKIE, path="/", secure=core.secure_cookie(request),
                           httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/me")
def me(request: Request, response: Response):
    u = core.require_reader(request)
    response.headers["Cache-Control"] = "no-store"
    if u["preview"]:
        return {**u, "cloudSharing": False, "opsAssistant": False}
    session = {} if u.get("openTest") else {"csrf": core.csrf_token(request.cookies[core.SESSION_COOKIE])}
    return {**u, **session,
            "dataRevision": _revision(),
            "cloudSharing": u["role"] == "admin" and cloud_login.enabled() and not core.public_preview_enabled(),
            "opsAssistant": ops_assistant.can_use(u) and any(ops_assistant.configured().values())}


# ---------------------------------------------------------------- 元数据（筛选项）

@router.get("/reference")
def reference(request: Request):
    core.require_reader(request)
    return {
        "weeks": [{"v": code, "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat()}
                  for code, (start, end) in REPORTING_WEEKS.items()],
        "trackStatuses": [{"status": status, "sub": sub, "label": label, "sub_label": sub_label}
                          for status, sub, label, sub_label in sorted(OFFICIAL_STATUS)],
    }


@router.get("/meta")
def meta(request: Request):
    preview = core.require_reader(request)["preview"]
    fact_meta = _fact_meta(core.snapshot_key())
    dimension_counts = dict(fact_meta["filterOptions"])
    rules, states, alert_channels = [], [], []
    with core.ops() as o:
        dimension_counts["channel_group"] = channel_groups.distribution(
            channel_groups.load(o)["groups"],
            ((row["v"], row["n"]) for row in dimension_counts["channel"]), fact_meta["range"]["orders"])
        if not preview:
            rules = [dict(r) for r in o.execute(
                f"SELECT rule, rule_cn, category, level, count(*) n FROM alerts WHERE {visible_alerts()} "
                "GROUP BY 1,2,3,4 ORDER BY n DESC").fetchall()]
            states = [dict(r) for r in o.execute(
                "SELECT coalesce(s.status,'open') status, count(*) n FROM alerts a "
                "LEFT JOIN alert_state s ON s.order_no=a.order_no AND s.rule=a.rule "
                f"WHERE {visible_alerts('a.carrier')} "
                "GROUP BY 1 ORDER BY n DESC").fetchall()]
            if "current_channel" in {row[1] for row in o.execute("PRAGMA table_info(alerts)")}:
                alert_channels = [{"v": value, "t": "未填写当前渠道" if value == "__empty__" else value, "n": count}
                                  for value, count in o.execute(f"""
                    SELECT {ALERT_CHANNEL_VALUE} AS channel, count(DISTINCT a.order_no) AS n
                    FROM alerts a WHERE {visible_alerts('a.carrier')}
                    GROUP BY 1 ORDER BY n DESC, channel
                """)]
    return {**fact_meta, "filterOptions": dimension_counts,
            "rules": rules, "alertStates": states, "alertChannels": alert_channels,
            "ruleOptions": [] if preview else [{"v": rule["key"], "t": rule["name"]} for rule in BUSINESS_RULES]}


@lru_cache(maxsize=1)
def _fact_meta(_snapshot):
    con = core.duck()
    try:
        facts = core.active_facts(core.order_facts(con))
        platform_names = core.platform_names()
        dimension_counts = {}
        for key, column in (("month", "创建月份"), ("wh", "仓库"), ("pf", "平台"),
                            ("cr", "物流商"), ("cn", "目的国"), ("lastmile", "C端承运商"),
                            ("channel", "当前渠道")):
            label = "coalesce(nullif(max(目的国中文),''),目的国)" if key == "cn" else f'"{column}"'
            sort = "value" if key == "month" else "n DESC, value"
            values = con.execute(f"""
                SELECT "{column}" AS value, {label} AS label, count(*) AS n FROM {facts} o
                WHERE coalesce("{column}",'') <> '' GROUP BY 1 ORDER BY {sort}
            """).fetchall()
            dimension_counts[key] = [{"v": v, "t": platform_names.get(v, label) if key == "pf" else label, "n": n}
                                     for v, label, n in values]
        week_start = next(iter(REPORTING_WEEKS.values()))[0]
        week_end = next(reversed(REPORTING_WEEKS.values()))[1]
        week_counts = dict(con.execute(f"""
            SELECT date_diff('day', ?, 创建日期) // 7, count(*) FROM {facts} o
            WHERE 创建日期 >= ? AND 创建日期 < ? GROUP BY 1
        """, [week_start, week_start, week_end]).fetchall())
        dimension_counts["week"] = []
        for index, (code, (start, exclusive_end)) in enumerate(REPORTING_WEEKS.items()):
            end = exclusive_end - timedelta(days=1)
            dimension_counts["week"].append({
                "v": code, "t": f"{code} · {start:%m/%d}–{end:%m/%d}", "n": week_counts.get(index, 0),
                "start": start.isoformat(), "end": end.isoformat(),
            })
        date_options = {}
        for basis in ("paid", "shipped"):
            column = DATE_BASIS_FIELDS[basis]
            days = con.execute(f"""
                SELECT CAST({column} AS DATE), count(*) FROM {facts} WHERE {column} IS NOT NULL GROUP BY 1
            """).fetchall()
            months, weeks = Counter(), Counter()
            for day, count in days:
                months[day.strftime("%Y-%m")] += count
                if week_start <= day < week_end:
                    weeks[(day - week_start).days // 7] += count
            date_options[basis] = {
                "month": [{"v": period, "t": period, "n": count} for period, count in sorted(months.items())],
                "week": [{**week, "n": weeks[index]} for index, week in enumerate(dimension_counts["week"])],
                "total": sum(months.values()),
            }
        missing_last_mile = con.execute(
            f"SELECT count(*) FROM {facts} WHERE coalesce(C端承运商,'')=''"
        ).fetchone()[0]
        if missing_last_mile:
            dimension_counts["lastmile"].append({"v": "__empty__", "t": "未填写 C 端渠道", "n": missing_last_mile})
            dimension_counts["lastmile"].sort(key=lambda row: (-row["n"], row["v"]))
        def col(sql):
            return [r[0] for r in con.execute(sql).fetchall() if r[0] not in (None, "")]

        whs = [row["v"] for row in dimension_counts["wh"]]
        pfs = [row["v"] for row in dimension_counts["pf"]]
        crs = [row["v"] for row in dimension_counts["cr"]]
        cns = [row["v"] for row in dimension_counts["cn"][:120]]
        months = [row["v"] for row in dimension_counts["month"]]
        sts = col(f"SELECT 状态, count(*) FROM {facts} GROUP BY 1 ORDER BY 2 DESC")
        last_mile = sorted(row["v"] for row in dimension_counts["lastmile"] if row["v"] != "__empty__")
        countries = [{"value": row["v"], "label": row["t"]} for row in dimension_counts["cn"]]
        tracking_statuses = con.execute("""
            SELECT 主状态 AS status, 主状态中文 AS label, 子状态 AS sub, 子状态中文 AS sub_label
            FROM dim_trk_status ORDER BY 主状态, 子状态
        """).df().to_dict("records")
        b = con.execute(f"SELECT min(创建日期), max(创建日期), count(*) FROM {facts}").fetchone()
    finally:
        con.close()

    return {
        "warehouses": whs, "platforms": pfs, "carriers": crs, "countries": cns,
        "months": months, "orderStatuses": sts,
        "lastMileCarriers": last_mile, "countryItems": countries, "trackStatuses": tracking_statuses,
        "filterOptions": dimension_counts, "dateOptions": date_options, "platformNames": platform_names,
        "range": {"start": str(b[0]), "end": str(b[1]), "orders": b[2]},
    }


# ---------------------------------------------------------------- 订单

def _multi(v: Optional[str]):
    return core.filter_values(v)


def _order_filters(month, wh, pf, cr, cn, status, q, signed, tracked, tracking="trk_status"):
    w, a = [], []

    def inlist(col, vals):
        if not vals:
            return
        ph = ",".join("?" * len(vals))
        w.append(f'"{col}" IN ({ph})')
        a.extend(vals)

    if month:
        w.append("创建月份 = ?"); a.append(month)
    inlist("仓库", _multi(wh))
    inlist("平台", _multi(pf))
    inlist("物流商", _multi(cr))
    inlist("目的国", _multi(cn))
    inlist("状态", _multi(status))
    if q:
        w.append("(履约单号 LIKE ? OR 快递单号 LIKE ? OR 订单号 LIKE ? OR 平台订单号 LIKE ?)")
        a.extend([f"%{q}%"] * 4)
    if signed in ("0", "1"):
        w.append("已签收 = ?"); a.append(int(signed))
    if tracked == "1":
        w.append(f"单号归一 IN (SELECT 单号 FROM {tracking})")
    elif tracked == "0":
        w.append(f"单号归一 NOT IN (SELECT 单号 FROM {tracking})")
    return (" WHERE " + " AND ".join(w)) if w else "", a


@router.get("/orders")
def list_orders(request: Request,
                page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                month: str = "", wh: str = "", pf: str = "", cr: str = "", cn: str = "",
                status: str = "", q: str = "", signed: str = "", tracked: str = "",
                sort: str = "创建时间", order: str = "desc"):
    core.require_user(request)
    safe_sort = sort if sort in (
        "创建时间", "签出时间", "签收时间", "仓内分钟", "物流分钟", "件数") else "创建时间"
    direction = "ASC" if order.lower() == "asc" else "DESC"
    off = (page - 1) * size

    con = core.duck()
    try:
        tracking, _, _ = tracking_archive.sources(con)
        where, args = _order_filters(month, wh, pf, cr, cn, status, q, signed, tracked, tracking)
        order_facts = delivery_facts(con, core.active_facts(core.order_facts(con)))
        facts = reporting_facts(con, order_facts)
        total = con.execute(f"SELECT count(*) FROM {order_facts}{where}", args).fetchone()[0]
        rows = con.execute(f"""
            SELECT 履约单号, 出库单号, 快递单号, 平台, 仓库, 物流商, C端承运商,
                   目的国, 目的国中文, 状态, 支付时间, 创建时间, 签出时间, 签收时间,
                   {DELIVERED_UTC} AS 承运商签收时间, 签收北京时间, 签收时区,
                   件数, 仓内分钟, 物流分钟, 已签收,
                   (单号归一 IN (SELECT 单号 FROM {tracking})) AS 有轨迹
            FROM {facts}{where}
            ORDER BY "{safe_sort}" {direction} NULLS LAST
            LIMIT ? OFFSET ?
        """, args + [size, off]).df()
    finally:
        con.close()

    return core.clean({"total": int(total), "page": page, "size": size,
                       "rows": rows.to_dict("records")})


@router.get("/orders/{order_no}")
def order_detail(order_no: str, request: Request, q: str = ""):
    preview = core.require_reader(request)["preview"]
    match = public_preview.lookup(q) if preview else ("", [])
    if not match:
        raise HTTPException(status_code=404, detail="履约单不存在")
    match_sql, match_args = match
    con = core.duck()
    try:
        orders = delivery_facts(con, core.active_facts(core.order_facts(con)))
        facts = reporting_facts(con, "selected_order", page_only=True)
        tracking, missing, archive = tracking_archive.sources(con, "SELECT 单号归一 FROM selected_order")
        row = con.execute(f"""
            WITH selected_order AS MATERIALIZED (
                SELECT o.* FROM {orders} o WHERE o.履约单号 = ?{match_sql}
            )
            SELECT o.*, t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
                   t.最新事件时间, t.最新事件当地时间, t.最新描述, t.地点,
                   o.{DELIVERED_UTC} AS 承运商签收时间,
                   CASE WHEN t.签收时间=o.{DELIVERED_UTC} THEN t.签收当地时间 END AS 签收当地时间,
                   t.运输天数, t.停滞天数, t.carrier AS 运输商代码,
                   coalesce(t.查询时间,h.查询时间) AS 查询时间, h.回补状态, h.carrier AS 回补运输商代码,
                   (t.单号 IS NULL AND o.单号归一 IN (SELECT 单号 FROM {missing})) AS 未注册
            FROM {facts} o LEFT JOIN {tracking} t ON t.单号 = o.单号归一
            LEFT JOIN {archive} h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
        """, [order_no, *match_args]).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="履约单不存在")
        cols = [d[0] for d in con.description]
        detail = dict(zip(cols, row))
        if core.columns(con, track_lookup.TABLE):
            status = con.execute(f"SELECT 状态标签,核对结果,状态,状态中文,子状态 FROM {track_lookup.TABLE} WHERE 履约单号=?", [order_no]).fetchone()
            if status:
                detail["状态标签"], detail["核对结果"], detail["轨迹状态"], detail["轨迹状态中文"], detail["轨迹子状态"] = status

        fields = core.columns(con, "履约单明细")
        detail_fields = [
            f"""any_value(nullif(trim("{column}"),'')) AS "{label}" """
            if column in fields else f'NULL AS "{label}"'
            for column, label in (("省、州", "目的州省"), ("城市", "目的城市"), ("邮编", "目的邮编"),
                                  ("履约单创建人", "履约单创建人"), ("履约单创建原因", "履约单创建原因"))
        ] if not preview else []
        if not preview:
            weight = 'max(try_cast("签出重/g" AS DOUBLE))' if "签出重/g" in fields else "NULL"
            detail_fields.append(f'{weight} AS "签出重/g"')
        steps = [f"max({column}) AS {label}" if column in fields else f"NULL AS {label}"
                 for column, label in (("分配时间_ts", "分配时间"), ("打包时间_ts", "打包时间"))]
        raw_delivery = "max(try_cast(签收时间 AS TIMESTAMP))" if "签收时间" in fields else "NULL"
        warehouse = con.execute(f"""
            SELECT {', '.join(detail_fields + steps)}, {raw_delivery} AS WMS妥投时间
            FROM 履约单明细 WHERE 履约单号=?
        """, [order_no]).fetchone()
        detail.update(zip((column[0] for column in con.description), warehouse))

        skus = [] if preview else con.execute(
            "SELECT sku, 商品名称, 数量, 批次码, 类目 FROM v_order_sku WHERE 履约单号 = ?",
            [order_no]).df().to_dict("records")

        events = []
        event_source = None
        milestones = []
        tn = detail.get("单号归一")
        verification = None if preview else tracking_freshness.order_check(con, tn)
        if tn:
            events = tracking_archive.events(con, tn, detail.get("回补运输商代码"))
            tracking_archive.fill_local_times(con, [detail])
            if events:
                event_source = "archive"
            milestone_clock = "当地时间" if "当地时间" in core.columns(con, "trk_milestone") else "NULL AS 当地时间"
            milestones = con.execute(f"""
                SELECT 节点, 时间, {milestone_clock} FROM trk_milestone WHERE 单号 = ? AND 时间 IS NOT NULL
                ORDER BY 时间
            """, [tn]).df().to_dict("records")
            event_columns = core.columns(con, "trk_event")
            if event_columns:
                provider = "事件承运商" if "事件承运商" in event_columns else "NULL AS 事件承运商"
                local_clock = "当地时间" if "当地时间" in event_columns else "NULL AS 当地时间"
                carrier_filter = " AND (运输商代码 = ? OR 运输商代码 IS NULL)" if "运输商代码" in event_columns else ""
                params = [tn, detail.get("运输商代码") or 0] if carrier_filter else [tn]
                sequence = ", 原始顺序 DESC NULLS LAST" if "原始顺序" in event_columns else ""
                current_events = con.execute(f"""
                    SELECT 事件时间, {local_clock}, 主状态, 子状态, 描述, 地点, {provider}
                    FROM trk_event WHERE 单号 = ?{carrier_filter}
                    ORDER BY 事件时间 ASC NULLS LAST{sequence}
                """, params).df().to_dict("records")
                if current_events:
                    event_source = "merged" if events else "current"
                    # Current polls overlap the archive. Keep each source's event order
                    # and omit events already represented in the current poll.
                    def identity(event):
                        return (
                            utc_key(event.get("事件时间")) or event.get("当地时间") or "",
                            *(str(event.get(key) or "").strip() for key in
                              ("子状态", "描述", "地点", "事件承运商")),
                        )

                    current_keys = {identity(event) for event in current_events}
                    events = [event for event in events if identity(event) not in current_keys] + current_events
                    events.sort(key=lambda event: utc_key(event.get("事件时间")) or "\uffff")
            delivery_key = utc_key(detail.get("承运商签收时间"))
            if delivery_key and not detail.get("签收当地时间"):
                for event in events:
                    if (utc_key(event.get("事件时间")) == delivery_key and event.get("当地时间")
                            and (event.get("主状态") == "Delivered"
                                 or (event.get("子状态") or "").startswith("Delivered_"))):
                        detail["签收当地时间"] = event["当地时间"]
                        break
    finally:
        con.close()

    if preview:
        return core.clean({
            "order": public_preview.select(detail, public_preview.DETAIL_FIELDS),
            "milestones": [m for m in milestones if m["节点"] in public_preview.MILESTONE_NAMES],
            "events": [public_preview.event(event) for event in events],
            "eventSource": event_source,
        })

    with core.ops() as o:
        notes = [dict(r) for r in o.execute(
            "SELECT ts, actor, body FROM order_notes WHERE order_no=? ORDER BY ts DESC",
            (order_no,)).fetchall()]
        alerts = [dict(r) for r in o.execute(
            f"""SELECT a.* FROM ({alert_history.all_rows()}) a
                WHERE a.order_no=? AND {visible_alerts('a.carrier')}
                ORDER BY coalesce(a.handle_at, a.cleared_at, a.synced_at) DESC NULLS LAST, a.rule""",
            (order_no,)).fetchall()]

    return core.clean({"order": detail, "skus": skus, "milestones": milestones, "events": events,
                       "eventSource": event_source,
                       "verification": verification,
                       "notes": notes, "alerts": alerts})


# ---------------------------------------------------------------- 预警

def _with_transport_times(rows):
    order_numbers = list({row["order_no"] for row in rows if row.get("shipped_at") or row.get("shipped_date")})
    if not order_numbers:
        return
    with core.duck() as con:
        facts = delivery_facts(con, core.active_facts(core.order_facts(con)))
        measured = reporting_facts(con, "selected_orders", page_only=True)
        delivered = "EXISTS (SELECT 1 FROM trk_status t WHERE t.单号=p.单号归一 AND t.状态='Delivered')"
        times = dict(con.execute(f"""
            WITH selected_orders AS MATERIALIZED (
                SELECT o.* FROM {facts} o WHERE o.履约单号 IN (SELECT unnest(?::VARCHAR[]))
            )
            SELECT p.履约单号, {transport_minutes('p', delivered)} FROM {measured} p
        """, [order_numbers]).fetchall())
    for row in rows:
        row["transport_minutes"] = times.get(row["order_no"])


def _export_alerts(rows, *, pending, scope, selected_rules):
    if not rows:
        raise HTTPException(404, "当前没有可导出的预警")

    platform_names = core.platform_names()
    beijing = timezone(timedelta(hours=8))

    def csv_cell(value):
        text = "" if value is None else str(value)
        if re.match(r"^\s*[=+@-]", text) or re.fullmatch(r"[0-9]{15,}", text):
            return "'" + text
        return text

    def duration(value):
        if value is None or not isfinite(value) or value < 0:
            return "—"
        hours, minutes = divmod(int(value), 60)
        days, remainder = divmod(hours, 24)
        if days:
            return f"{days} 天 {remainder} 小时"
        return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"

    def handled_time(value):
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return value
        if moment.tzinfo is not None:
            moment = moment.astimezone(beijing)
        return moment.isoformat(sep=" ", timespec="minutes")[:16]

    output = StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    handler_columns = [] if pending else ["处理人", "归档时间"]
    writer.writerow(["履约单号", "运单号", "平台", "平台代码", "预警", "履约出库时间（北京时间）",
                     "级别", "运输时间", *handler_columns, "当前物流渠道", "后台物流商", "异常持续天数"])
    for row in rows:
        platform = row.get("platform_code")
        handler = [] if pending else [row.get("handled_by"), handled_time(row.get("handle_at"))]
        writer.writerow(map(csv_cell, (
            row["order_no"], row["tracking_no"], platform_names.get(platform, platform), platform,
            row["rule_cn"], row["shipped_at"] or row["shipped_date"], row["level"],
            duration(row.get("transport_minutes")), *handler, row["current_channel"], row["carrier"], row["days"],
        )))

    title = "预警处理" if pending else "归档预警"
    rule = next((item["name"] for item in BUSINESS_RULES if selected_rules == [item["key"]]), None)
    suffix = "本页" if scope == "page" else "全部命中"
    filename = "-".join(part for part in (title, rule, suffix) if part) + ".csv"
    return Response(
        b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                 "Cache-Control": "no-store"},
    )


@router.get("/alerts")
def list_alerts(request: Request,
                page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                rule: str = "", level: str = "", state: str = "", assignee: str = "",
                q: str = "", wh: str = "", pf: str = "", cr: str = "", cn: str = "",
                month: str = "", sort: str = "shipped", start: str = "", end: str = "",
                main_only: bool = False, channel: str = "", lastmile: str = "", week: str = "",
                channel_group: str = "",
                tstat: str = "", sub: str = "",
                date_basis: DateBasis = DEFAULT_DATE_BASIS,
                view: Literal["pending", "archive"] = "pending",
                export: Literal["page", "all"] | None = None):
    core.require_user(request)
    bounds = time_bounds(start, end)
    pending = view == "pending"
    mode_filter = f"a.state IN {alert_history.PENDING_STATES}" if pending else "a.state='archived'"
    w, a = [visible_alerts("a.carrier"), mode_filter], []

    def inlist(col, vals):
        if not vals:
            return
        ph = ",".join("?" * len(vals))
        w.append(f"{col} IN ({ph})")
        a.extend(vals)

    selected_rules = _multi(rule)
    inlist("a.rule", selected_rules)
    inlist("a.level", _multi(level))
    inlist("a.warehouse", _multi(wh))
    inlist("a.carrier", _multi(cr))
    inlist("a.country", _multi(cn))
    inlist("a.track_status", _multi(tstat))
    inlist("a.track_sub", _multi(sub))
    if main_only:
        w.append("a.is_main = 1")
    if assignee:
        w.append("a.handled_by = ?"); a.append(assignee)
    inlist("a.state", _multi(state))
    off = (page - 1) * size
    records = alert_history.current_rows() if pending else alert_history.all_rows()
    base = f"FROM ({records}) a"
    with core.ops() as o:
        columns = {row[1] for row in o.execute("PRAGMA table_info(alerts)")}
        has_timestamp = "shipped_at" in columns
        has_channel = "current_channel" in columns
        inlist(ALERT_CHANNEL_VALUE if has_channel else "'__empty__'", _multi(channel))
        if channel_group:
            selection = channel_groups.filter_values(channel_group, channel_groups.load(o)["groups"])
            group_sql, group_args = channel_groups.predicate("a.current_channel", selection, sqlite=True)
            if group_sql:
                w.append(group_sql)
                a.extend(group_args)
        inlist("a.platform_code" if "platform_code" in columns else "NULL", _multi(pf))
        carrier = f"coalesce(nullif({core.last_mile_sql('a.last_mile')},''),'__empty__')" if "last_mile" in columns else "'__empty__'"
        inlist(carrier, core.last_mile_filter_values(lastmile))
        prefix = date_basis or DEFAULT_DATE_BASIS
        stamp = f"{prefix}_at"
        timestamp_column = f"replace(a.{stamp},'T',' ')" if stamp in columns else "NULL"
        calendar_day = f"substr({timestamp_column},1,10)"
        if f"{prefix}_date" in columns:
            calendar_day = f"coalesce(a.{prefix}_date,{calendar_day})"
        inlist(f"substr({calendar_day},1,7)", _multi(month))
        week_sql, week_args = week_filter(calendar_day, _multi(week))
        if week_sql:
            w.append(week_sql)
            a.extend(day.isoformat() for day in week_args)
        for operator, bound in zip((">=", "<"), bounds):
            if isinstance(bound, datetime):
                w.append(f"{timestamp_column} {operator} ?")
                a.append(bound.isoformat(sep=" ", timespec="seconds"))
            elif bound is not None:
                w.append(f"{calendar_day} {operator} ?")
                a.append(bound.isoformat())
        query = normalize("NFKC", q).strip().upper()
        if query:
            search_fields = [f"a.{field}" for field in
                             ("order_no", "tracking_no", "customer_order_no", "platform_order_no") if field in columns]
            search = [f"instr(upper({field}), ?) > 0" for field in search_fields]
            a.extend([query] * len(search_fields))
            waybill = normalize_waybill(query)
            if waybill and waybill != query:
                search.append("instr(upper(a.tracking_no), ?) > 0")
                a.append(waybill)
            w.append("(" + " OR ".join(search) + ")")
        where = " WHERE " + " AND ".join(w)
        shipped = "coalesce(a.shipped_at,a.shipped_date)" if has_timestamp else "a.shipped_date"
        ordering = {
            "days": "a.days DESC",
            "level": "CASE a.level WHEN '紧急' THEN 0 WHEN '高' THEN 1 ELSE 2 END, a.days DESC",
            "order": "a.order_no",
        }
        default_sort = f"{shipped} DESC NULLS LAST" if pending else "a.handle_at DESC NULLS LAST"
        order_by = ordering.get(sort, default_sort) + ", a.order_no, a.rule"
        shipped_column = "" if has_timestamp else ", NULL AS shipped_at"
        channel_column = "" if has_channel else ", NULL AS current_channel"
        limit = "" if export == "all" else " LIMIT ? OFFSET ?"
        row_args = a if export == "all" else a + [size, off]
        rows = [dict(r) for r in o.execute(
            f"SELECT a.*{shipped_column}{channel_column} "
            f"{base}{where} ORDER BY {order_by}{limit}", row_args).fetchall()]
        if export is None:
            total = o.execute(f"SELECT count(*) {base}{where}", a).fetchone()[0]
            summ = [dict(r) for r in o.execute(
                f"SELECT a.rule, a.rule_cn, a.level, count(*) n {base}{where} GROUP BY 1,2,3 ORDER BY n DESC",
                a).fetchall()]
    _with_transport_times(rows)
    if export is not None:
        return _export_alerts(rows, pending=pending, scope=export, selected_rules=selected_rules)
    return {"total": total, "page": page, "size": size, "rows": rows, "summary": summ}


class AlertTarget(BaseModel):
    order_no: str
    rule: str
    first_seen_at: str | None = None
    history_id: int | None = None
    handle_at: str | None = None


class ActionIn(AlertTarget):
    action: str
    note: str = Field(default="", max_length=2000)


class BatchActionIn(BaseModel):
    action: str
    items: list[AlertTarget] = Field(min_length=1, max_length=100)


def undo_alert(o, item, state):
    if item.history_id is None and item.handle_at is None:
        raise HTTPException(status_code=422, detail="请刷新列表后再撤销")
    row = o.execute(f"""
        SELECT * FROM ({alert_history.all_rows()})
        WHERE order_no=? AND rule=? AND state=? AND handle_at IS ? AND {visible_alerts()}
          AND ((? IS NOT NULL AND history_id=?)
             OR (? IS NULL AND first_seen_at IS ?))
        ORDER BY is_current DESC LIMIT 1
    """, (item.order_no, item.rule, state, item.handle_at,
          item.history_id, item.history_id, item.history_id, item.first_seen_at)).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="这条预警的处理记录已变化，请刷新后重试")
    if row["is_current"]:
        o.execute("DELETE FROM alert_state WHERE order_no=? AND rule=?", (item.order_no, item.rule))
    else:
        o.execute("""UPDATE alert_history SET state='recovered', handled_by=NULL, handle_at=NULL, handle_note=NULL
                     WHERE rowid=?""", (row["history_id"],))
    return row


def apply_alert_action(user, action, items, note=""):
    states = {"resolve": "resolved", "archive": "archived",
              "undo_resolve": "resolved", "undo_archive": "archived"}
    if action not in states:
        raise HTTPException(status_code=400, detail="不支持的预警动作")
    if states[action] == "archived" and user["role"] not in ("admin", "logistics"):
        raise HTTPException(status_code=403, detail="归档需要物流或管理员权限")
    target_status = states[action]
    handler = user["display_name"] or user["username"]
    handle_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    targets = {(item.order_no, item.rule, item.history_id): item for item in items}
    undo = action.startswith("undo_")

    with core.ops() as o:
        o.execute("BEGIN IMMEDIATE")
        if undo:
            restored = 0
            for item in targets.values():
                previous = undo_alert(o, item, target_status)
                restored += previous["is_current"]
                core.audit(user["username"], f"alert_{action}", f"{item.order_no}/{item.rule}",
                           {"handled_by": handler, "previous_handler": previous["handled_by"],
                            "previous_handle_at": previous["handle_at"], "previous_note": previous["handle_note"],
                            "first_seen_at": previous["first_seen_at"]}, connection=o)
            return {"ok": True, "updated": len(targets), "restored": restored}
        if not all(o.execute(f"""
            SELECT 1 FROM ({alert_history.current_rows()})
            WHERE order_no=? AND rule=? AND {visible_alerts()} AND state IN {alert_history.PENDING_STATES}
              AND (? IS NULL OR first_seen_at=?)
        """, (item.order_no, item.rule, item.first_seen_at, item.first_seen_at)).fetchone()
                   for item in targets.values()):
            raise HTTPException(status_code=409, detail="至少一条预警已结束，请刷新列表后重试")
        for item in targets.values():
            o.execute("""
                INSERT INTO alert_state (order_no,rule,status,note,updated_at,updated_by,handler_name)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(order_no,rule) DO UPDATE SET
                  status = excluded.status,
                  note = coalesce(nullif(excluded.note,''), alert_state.note),
                  updated_at = excluded.updated_at, updated_by = excluded.updated_by, handler_name = excluded.handler_name
            """, (item.order_no, item.rule, target_status, note, handle_at, user["username"], handler))
            core.audit(user["username"], f"alert_{action}", f"{item.order_no}/{item.rule}",
                       {"note": note, "handled_by": handler}, connection=o)
    return {"ok": True, "status": target_status, "handled_by": handler,
            "handle_at": handle_at, "updated": len(targets)}


@router.post("/alerts/action")
def alert_action(body: ActionIn, request: Request):
    return apply_alert_action(core.require_editor(request), body.action, [body], body.note)


@router.post("/alerts/batch")
def batch_alert_action(body: BatchActionIn, request: Request):
    return apply_alert_action(core.require_editor(request), body.action, body.items)


@router.post("/orders/{order_no}/notes")
def add_note(order_no: str, body: dict, request: Request):
    u = core.require_editor(request)
    text = (body or {}).get("body", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="备注不能为空")
    con = core.duck()
    try:
        exists = con.execute(f"SELECT 1 FROM {core.active_facts()} WHERE 履约单号 = ?", [order_no]).fetchone()
    finally:
        con.close()
    if not exists:
        raise HTTPException(status_code=404, detail="履约单不存在")
    with core.ops() as o:
        o.execute("INSERT INTO order_notes (order_no, ts, actor, body) VALUES (?,?,?,?)",
                  (order_no, core.now_iso(), u["username"], text[:2000]))
        core.audit(u["username"], "note", order_no, {"body": text[:200]}, connection=o)
    return {"ok": True}


# ---------------------------------------------------------------- 统计

@router.get("/stats")
def stats(request: Request, month: str = "", wh: str = "", pf: str = "",
          cr: str = "", cn: str = "", status: str = ""):
    core.require_user(request)
    where, args = _order_filters(month, wh, pf, cr, cn, status, "", "", "")
    con = core.duck()
    try:
        facts = reporting_facts(con, delivery_facts(con, core.active_facts()))
        r = con.execute(f"""
            SELECT count(*) n,
                   coalesce(sum(件数),0) items,
                   count({DELIVERED_BJ}) sign,
                   avg(仓内分钟) FILTER (WHERE 仓内分钟 > 0) hm,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0) tm,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 4320) s3,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 7200) s5,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 10080) s7,
                   count(*) FILTER (WHERE 物流分钟 > 0) sg
            FROM {facts}{where}
        """, args).fetchone()
        months = con.execute(f"""
            SELECT 创建月份 m, count(*) n, count({DELIVERED_BJ}) s,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 d
            FROM {facts}{where} GROUP BY 1 ORDER BY 1
        """, args).df().to_dict("records")
        by_carrier = con.execute(f"""
            SELECT 物流商 k, count(*) n, count({DELIVERED_BJ}) s,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 d
            FROM {facts}{where} GROUP BY 1 ORDER BY n DESC LIMIT 20
        """, args).df().to_dict("records")
        by_wh = con.execute(f"""
            SELECT 仓库 k, count(*) n, count({DELIVERED_BJ}) s,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 d
            FROM {facts}{where} GROUP BY 1 ORDER BY n DESC
        """, args).df().to_dict("records")
    finally:
        con.close()

    n = r[0] or 0
    return core.clean({
        "orders": n, "items": int(r[1] or 0),
        "signRate": (100.0 * (r[2] or 0) / n) if n else None,
        "inboundHours": (r[3] / 60.0) if r[3] else None,
        "transitDays": (r[4] / 1440.0) if r[4] else None,
        "rate3": (100.0 * r[5] / r[8]) if r[8] else None,
        "rate5": (100.0 * r[6] / r[8]) if r[8] else None,
        "rate7": (100.0 * r[7] / r[8]) if r[8] else None,
        "months": months, "byCarrier": by_carrier, "byWarehouse": by_wh,
    })


def _alert_activity():
    with core.ops() as o:
        a = o.execute(f"SELECT count(*) FROM alerts WHERE {visible_alerts()}").fetchone()[0]
        pending = o.execute(f"""
            SELECT count(DISTINCT a.order_no) FROM alerts a LEFT JOIN alert_state s
            ON a.order_no=s.order_no AND a.rule=s.rule
            WHERE {visible_alerts('a.carrier')} AND coalesce(s.status,'open') IN ('open','claimed','handling')
        """).fetchone()[0]
        last = o.execute("SELECT job, started_at, status FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    return a, pending, dict(last) if last else None


def _recent_imports(con, limit):
    if not core.columns(con, "sync_ledger"):
        return []
    return con.execute("""
        SELECT filename, rows, orders, ingested_at FROM sync_ledger
        ORDER BY ingested_at DESC, filename LIMIT ?
    """, [limit]).df().to_dict("records")


@router.get("/activity")
def activity(request: Request):
    """Header indicators without scanning historical events."""
    core.require_user(request)
    _, pending, _ = _alert_activity()
    with core.duck() as con:
        imports = _recent_imports(con, 1)
    return core.clean({"pendingOrders": pending, "lastImportAt": imports[0]["ingested_at"] if imports else None})


@router.get("/health")
def health(request: Request):
    core.require_user(request)
    a, pending, last = _alert_activity()
    return {**_fact_health(core.snapshot_key()), "alerts": a, "pendingOrders": pending, "lastJob": last}


@lru_cache(maxsize=1)
def _fact_health(_snapshot):
    con = core.duck()
    try:
        tracking, missing_records, archive = tracking_archive.sources(con)
        facts = core.active_facts()
        orders, first_day, last_day = con.execute(
            f"SELECT count(*), min(创建日期), max(创建日期) FROM {facts}"
        ).fetchone()
        con.execute(f"""
            CREATE TEMP TABLE active_waybills AS
            SELECT DISTINCT 单号归一 AS 单号 FROM {facts} WHERE coalesce(单号归一,'')<>''
        """)
        track = con.execute(f"""
            SELECT count(DISTINCT t.单号) AS records,
                   count(DISTINCT t.单号) FILTER (WHERE 最新事件时间 IS NOT NULL) AS with_event,
                   max(查询时间) AS last_check
            FROM {tracking} t SEMI JOIN active_waybills a ON a.单号=t.单号
        """).fetchone()
        missing = con.execute(f"""
            SELECT count(DISTINCT m.单号) FROM {missing_records} m
            SEMI JOIN active_waybills a ON a.单号=m.单号 WHERE NOT EXISTS
                (SELECT 1 FROM {tracking} t WHERE t.单号=m.单号)
        """).fetchone()[0]
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        history = con.execute(f"""
            SELECT count(DISTINCT h.单号) FILTER (WHERE 回补状态='empty'),
                   count(DISTINCT h.单号) FILTER (WHERE 事件数>0),
                   coalesce(sum(事件数),0), max(查询时间)
            FROM {archive} h SEMI JOIN {facts} o
              ON o.履约单号=h.履约单号 AND o.单号归一=h.单号
        """).fetchone()
        native = con.execute(f"""
            SELECT count(DISTINCT e.单号), count(*) FROM trk_event e
            SEMI JOIN active_waybills a ON a.单号=e.单号
            WHERE NOT EXISTS (
                SELECT 1 FROM {archive} h SEMI JOIN {facts} o
                  ON o.履约单号=h.履约单号 AND o.单号归一=h.单号
                WHERE h.单号=e.单号 AND h.事件数>0
            )
        """).fetchone() if "trk_event" in tables else (0, 0)
        imports = _recent_imports(con, 10)
    finally:
        con.close()
    return core.clean({"orders": orders,
                       "range": {"start": str(first_day), "end": str(last_day)},
                       "recentImports": imports,
                       "tracking": {"records": track[0] + history[0], "withEvent": track[1],
                                    "unmatched": missing, "empty": history[0],
                                    "lastCheck": max((d for d in (track[2], history[3]) if d), default=None),
                                    "detailedNumbers": native[0] + history[1],
                                    "detailedEvents": native[1] + history[2],
                                    "archiveNumbers": history[1], "archiveEvents": history[2]}})
