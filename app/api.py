"""REST API：订单查询、预警处理、统计。"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import cloud_login, core, ops_auth, ops_assistant, public_preview
from .carrier_delivery import DELIVERED_BJ, DELIVERED_UTC, reporting_facts
from .order_time import delivery_facts
from . import tracking_archive
from .rules import RULES as BUSINESS_RULES, visible_alerts
from .tracking_clock import utc_key

router = APIRouter(prefix="/api")
ALERT_CHANNEL_VALUE = "coalesce(nullif(a.current_channel,''),'__empty__')"

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
            "cloudSharing": u["role"] == "admin" and cloud_login.enabled() and not core.public_preview_enabled(),
            "opsAssistant": ops_assistant.can_use(u) and any(ops_assistant.configured().values())}


# ---------------------------------------------------------------- 元数据（筛选项）

@router.get("/meta")
def meta(request: Request):
    preview = core.require_reader(request)["preview"]
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

    rules, states, alert_channels = [], [], []
    if not preview:
        with core.ops() as o:
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

    return {
        "warehouses": whs, "platforms": pfs, "carriers": crs, "countries": cns,
        "months": months, "orderStatuses": sts,
        "lastMileCarriers": last_mile, "countryItems": countries, "trackStatuses": tracking_statuses,
        "filterOptions": dimension_counts, "platformNames": platform_names,
        "rules": rules, "alertStates": states, "alertChannels": alert_channels,
        "ruleOptions": [] if preview else [{"v": rule["key"], "t": rule["name"]} for rule in BUSINESS_RULES],
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
        order_facts = delivery_facts(con, core.active_facts())
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

        fields = core.columns(con, "履约单明细")
        address = [
            f"""any_value(nullif(trim("{column}"),'')) AS "{label}" """
            if column in fields else f'NULL AS "{label}"'
            for column, label in (("省、州", "目的州省"), ("城市", "目的城市"), ("邮编", "目的邮编"))
        ] if not preview else []
        steps = [f"max({column}) AS {label}" if column in fields else f"NULL AS {label}"
                 for column, label in (("分配时间_ts", "分配时间"), ("打包时间_ts", "打包时间"))]
        raw_delivery = "max(try_cast(签收时间 AS TIMESTAMP))" if "签收时间" in fields else "NULL"
        warehouse = con.execute(f"""
            SELECT {', '.join(address + steps)}, {raw_delivery} AS WMS妥投时间
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
            "SELECT a.*, coalesce(s.status,'open') state, s.assignee, s.note AS handle_note, "
            "s.updated_at FROM alerts a LEFT JOIN alert_state s "
            f"ON s.order_no=a.order_no AND s.rule=a.rule WHERE a.order_no=? AND {visible_alerts('a.carrier')}",
            (order_no,)).fetchall()]

    return core.clean({"order": detail, "skus": skus, "milestones": milestones, "events": events,
                       "eventSource": event_source,
                       "notes": notes, "alerts": alerts})


# ---------------------------------------------------------------- 预警

@router.get("/alerts")
def list_alerts(request: Request,
                page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                rule: str = "", level: str = "", state: str = "", assignee: str = "",
                q: str = "", wh: str = "", cr: str = "", cn: str = "",
                month: str = "", sort: str = "shipped", start: str = "", end: str = "",
                main_only: bool = False, channel: str = ""):
    core.require_user(request)
    w, a = [visible_alerts("a.carrier")], []

    def inlist(col, vals):
        if not vals:
            return
        ph = ",".join("?" * len(vals))
        w.append(f"{col} IN ({ph})")
        a.extend(vals)

    inlist("a.rule", _multi(rule))
    inlist("a.level", _multi(level))
    inlist("a.warehouse", _multi(wh))
    inlist("a.carrier", _multi(cr))
    inlist("a.country", _multi(cn))
    inlist("substr(a.created_date,1,7)", _multi(month))
    if start:
        w.append("a.created_date >= ?"); a.append(start)
    if end:
        w.append("a.created_date <= ?"); a.append(end)
    if main_only:
        w.append("a.is_main = 1")
    if assignee:
        w.append("s.assignee = ?"); a.append(assignee)
    if state:
        if state == "open":
            w.append("coalesce(s.status,'open') = 'open'")
        else:
            w.append("coalesce(s.status,'open') IN (%s)" % ",".join("?" * len(_multi(state))))
            a.extend(_multi(state))
    if q:
        w.append("(instr(upper(a.order_no), ?) > 0 OR instr(upper(a.tracking_no), ?) > 0)")
        a.extend([q.strip().upper()] * 2)

    off = (page - 1) * size
    base = ("FROM alerts a LEFT JOIN alert_state s "
            "ON s.order_no=a.order_no AND s.rule=a.rule")
    with core.ops() as o:
        columns = {row[1] for row in o.execute("PRAGMA table_info(alerts)")}
        has_timestamp = "shipped_at" in columns
        has_channel = "current_channel" in columns
        inlist(ALERT_CHANNEL_VALUE if has_channel else "'__empty__'", _multi(channel))
        where = " WHERE " + " AND ".join(w)
        shipped = "coalesce(a.shipped_at,a.shipped_date)" if has_timestamp else "a.shipped_date"
        ordering = {
            "days": "a.days DESC",
            "level": "CASE a.level WHEN '紧急' THEN 0 WHEN '高' THEN 1 ELSE 2 END, a.days DESC",
            "order": "a.order_no",
        }
        order_by = ordering.get(sort, f"{shipped} DESC NULLS LAST") + ", a.order_no, a.rule"
        shipped_column = "" if has_timestamp else ", NULL AS shipped_at"
        channel_column = "" if has_channel else ", NULL AS current_channel"
        total = o.execute(f"SELECT count(*) {base}{where}", a).fetchone()[0]
        rows = [dict(r) for r in o.execute(
            f"SELECT a.*{shipped_column}{channel_column}, coalesce(s.status,'open') state, s.assignee, s.updated_at handle_at "
            f"{base}{where} ORDER BY {order_by} LIMIT ? OFFSET ?", a + [size, off]).fetchall()]
        summ = [dict(r) for r in o.execute(
            f"SELECT a.rule, a.rule_cn, a.level, count(*) n {base}{where} GROUP BY 1,2,3 ORDER BY n DESC",
            a).fetchall()]
    return {"total": total, "page": page, "size": size, "rows": rows, "summary": summ}


class AlertTarget(BaseModel):
    order_no: str
    rule: str


class ActionIn(AlertTarget):
    action: str
    note: str = Field(default="", max_length=2000)
    assignee: str = ""


class BatchActionIn(BaseModel):
    action: str
    items: list[AlertTarget] = Field(min_length=1, max_length=100)


def apply_alert_action(user, action, items, note="", assignee=""):
    states = {"claim": "claimed", "resolve": "resolved", "archive": "archived",
              "reopen": "open", "handling": "handling", "note": None}
    if action not in states:
        raise HTTPException(status_code=400, detail="不支持的预警动作")
    if action in ("archive", "reopen") and user["role"] not in ("admin", "logistics"):
        raise HTTPException(status_code=403, detail="归档和重开需要物流或管理员权限")
    target_status = states[action]
    owner = assignee or (user["username"] if action in ("claim", "handling") else None)
    targets = list(dict.fromkeys((item.order_no, item.rule) for item in items))

    with core.ops() as o:
        o.execute("BEGIN IMMEDIATE")
        if not all(o.execute("SELECT 1 FROM alerts WHERE order_no=? AND rule=?", target).fetchone()
                   for target in targets):
            raise HTTPException(status_code=404, detail="至少一条预警已变更，请刷新列表后重试")
        for order_no, rule in targets:
            o.execute("""
                INSERT INTO alert_state (order_no,rule,status,assignee,note,updated_at,updated_by)
                VALUES (?,?,coalesce(?, 'open'),?,?,?,?)
                ON CONFLICT(order_no,rule) DO UPDATE SET
                  status = coalesce(?, alert_state.status),
                  assignee = coalesce(excluded.assignee, alert_state.assignee),
                  note = coalesce(nullif(excluded.note,''), alert_state.note),
                  updated_at = excluded.updated_at, updated_by = excluded.updated_by
            """, (order_no, rule, target_status, owner, note, core.now_iso(), user["username"], target_status))
            core.audit(user["username"], f"alert_{action}", f"{order_no}/{rule}",
                       {"note": note}, connection=o)
    return {"ok": True, "status": target_status, "assignee": owner, "updated": len(targets)}


@router.post("/alerts/action")
def alert_action(body: ActionIn, request: Request):
    return apply_alert_action(core.require_editor(request), body.action, [body], body.note, body.assignee)


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
