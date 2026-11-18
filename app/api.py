"""REST API：订单查询、预警处理、统计。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from . import core

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------- 认证

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    import sqlite3

    from init_ops import verify_pw

    with core.ops() as con:
        row = con.execute("SELECT username,password_hash,active,display_name,role FROM users WHERE username=?",
                          (body.username,)).fetchone()
    if not row or not row["active"] or not verify_pw(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    sid, exp = core.create_session(body.username,
                                   ip=request.client.host if request.client else "",
                                   ua=request.headers.get("user-agent", ""))
    core.audit(body.username, "login", body.username)
    response.set_cookie(core.SESSION_COOKIE, sid, httponly=True, samesite="lax",
                        max_age=core.SESSION_TTL_HOURS * 3600, path="/")
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
    response.delete_cookie(core.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    u = core.current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="未登录")
    return u


# ---------------------------------------------------------------- 元数据（筛选项）

@router.get("/meta")
def meta(request: Request):
    core.require_user(request)
    con = core.duck()
    try:
        def col(sql):
            return [r[0] for r in con.execute(sql).fetchall() if r[0] not in (None, "")]

        whs = col("SELECT 仓库, any_value(c) FROM (SELECT 仓库, count(*) c FROM fct_order "
                  "WHERE 仓库 IS NOT NULL GROUP BY 1) t GROUP BY 1 ORDER BY 2 DESC")
        pfs = col("SELECT 平台, any_value(c) FROM (SELECT 平台, count(*) c FROM fct_order "
                  "WHERE 平台 IS NOT NULL GROUP BY 1) t GROUP BY 1 ORDER BY 2 DESC")
        crs = col("SELECT 物流商, any_value(c) FROM (SELECT 物流商, count(*) c FROM fct_order "
                  "WHERE 物流商 IS NOT NULL GROUP BY 1) t GROUP BY 1 ORDER BY 2 DESC")
        cns = col("SELECT 目的国, any_value(c) FROM (SELECT 目的国, count(*) c FROM fct_order "
                  "WHERE 目的国 IS NOT NULL GROUP BY 1) t GROUP BY 1 ORDER BY 2 DESC LIMIT 120")
        months = col("SELECT DISTINCT 创建月份 FROM fct_order ORDER BY 1")
        sts = col("SELECT 状态, any_value(c) FROM (SELECT 状态, count(*) c FROM fct_order "
                  "WHERE 状态 IS NOT NULL GROUP BY 1) t GROUP BY 1 ORDER BY 2 DESC")
        b = con.execute("SELECT min(创建日期), max(创建日期), count(*) FROM fct_order").fetchone()
    finally:
        con.close()

    with core.ops() as o:
        rules = [dict(r) for r in o.execute(
            "SELECT rule, rule_cn, category, level, count(*) n FROM alerts "
            "GROUP BY 1,2,3,4 ORDER BY n DESC").fetchall()]
        states = [dict(r) for r in o.execute(
            "SELECT coalesce(s.status,'open') status, count(*) n FROM alerts a "
            "LEFT JOIN alert_state s ON s.order_no=a.order_no AND s.rule=a.rule "
            "GROUP BY 1 ORDER BY n DESC").fetchall()]

    return {
        "warehouses": whs, "platforms": pfs, "carriers": crs, "countries": cns,
        "months": months, "orderStatuses": sts,
        "rules": rules, "alertStates": states,
        "range": {"start": str(b[0]), "end": str(b[1]), "orders": b[2]},
    }


# ---------------------------------------------------------------- 订单

def _multi(v: Optional[str]):
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


def _order_filters(month, wh, pf, cr, cn, status, q, signed, tracked):
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
        w.append("(履约单号 LIKE ? OR 快递单号 LIKE ? OR 订单号 LIKE ?)")
        a.extend([f"%{q}%"] * 3)
    if signed in ("0", "1"):
        w.append("已签收 = ?"); a.append(int(signed))
    if tracked == "1":
        w.append("单号归一 IN (SELECT 单号 FROM trk_status)")
    elif tracked == "0":
        w.append("单号归一 NOT IN (SELECT 单号 FROM trk_status)")
    return (" WHERE " + " AND ".join(w)) if w else "", a


@router.get("/orders")
def list_orders(request: Request,
                page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                month: str = "", wh: str = "", pf: str = "", cr: str = "", cn: str = "",
                status: str = "", q: str = "", signed: str = "", tracked: str = "",
                sort: str = "创建时间", order: str = "desc"):
    core.require_user(request)
    where, args = _order_filters(month, wh, pf, cr, cn, status, q, signed, tracked)
    safe_sort = sort if sort in (
        "创建时间", "签出时间", "签收时间", "仓内分钟", "物流分钟", "件数") else "创建时间"
    direction = "ASC" if order.lower() == "asc" else "DESC"
    off = (page - 1) * size

    con = core.duck()
    try:
        total = con.execute(f"SELECT count(*) FROM fct_order{where}", args).fetchone()[0]
        rows = con.execute(f"""
            SELECT 履约单号, 出库单号, 快递单号, 平台, 仓库, 物流商, C端承运商,
                   目的国, 目的国中文, 状态, 创建时间, 签出时间, 签收时间,
                   件数, 仓内分钟, 物流分钟, 已签收,
                   (单号归一 IN (SELECT 单号 FROM trk_status)) AS 有轨迹
            FROM fct_order{where}
            ORDER BY "{safe_sort}" {direction} NULLS LAST
            LIMIT ? OFFSET ?
        """, args + [size, off]).df()
    finally:
        con.close()

    return {"total": int(total), "page": page, "size": size,
            "rows": rows.to_dict("records")}


@router.get("/orders/{order_no}")
def order_detail(order_no: str, request: Request):
    core.require_user(request)
    con = core.duck()
    try:
        row = con.execute("""
            SELECT o.*, t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
                   t.最新事件时间, t.最新描述, t.地点, t.签收时间 AS 承运商签收时间,
                   t.运输天数, t.停滞天数,
                   (o.单号归一 IN (SELECT 单号 FROM trk_missing)) AS 未注册
            FROM fct_order o LEFT JOIN trk_status t ON t.单号 = o.单号归一
            WHERE o.履约单号 = ?
        """, [order_no]).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="履约单不存在")
        cols = [d[0] for d in con.description]
        detail = dict(zip(cols, row))

        skus = con.execute(
            "SELECT sku, 商品名称, 数量, 批次码, 类目 FROM v_order_sku WHERE 履约单号 = ?",
            [order_no]).df().to_dict("records")

        events = []
        tn = detail.get("单号归一")
        if tn:
            events = con.execute("""
                SELECT 节点, 时间 FROM trk_milestone WHERE 单号 = ? AND 时间 IS NOT NULL
                ORDER BY 时间
            """, [tn]).df().to_dict("records")
    finally:
        con.close()

    with core.ops() as o:
        notes = [dict(r) for r in o.execute(
            "SELECT ts, actor, body FROM order_notes WHERE order_no=? ORDER BY ts DESC",
            (order_no,)).fetchall()]
        alerts = [dict(r) for r in o.execute(
            "SELECT a.*, coalesce(s.status,'open') state, s.assignee, s.note AS handle_note, "
            "s.updated_at FROM alerts a LEFT JOIN alert_state s "
            "ON s.order_no=a.order_no AND s.rule=a.rule WHERE a.order_no=?", (order_no,)).fetchall()]

    return {"order": detail, "skus": skus, "milestones": events,
            "notes": notes, "alerts": alerts}


# ---------------------------------------------------------------- 预警

@router.get("/alerts")
def list_alerts(request: Request,
                page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                rule: str = "", level: str = "", state: str = "", assignee: str = "",
                q: str = "", wh: str = "", cr: str = "", cn: str = "",
                month: str = "", sort: str = "days"):
    core.require_user(request)
    w, a = [], []

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
    if month:
        w.append("substr(a.created_date,1,7) = ?"); a.append(month)
    if assignee:
        w.append("s.assignee = ?"); a.append(assignee)
    if state:
        if state == "open":
            w.append("coalesce(s.status,'open') = 'open'")
        else:
            w.append("coalesce(s.status,'open') IN (%s)" % ",".join("?" * len(_multi(state))))
            a.extend(_multi(state))
    if q:
        w.append("(a.order_no LIKE ? OR a.tracking_no LIKE ?)")
        a.extend([f"%{q}%"] * 2)

    where = (" WHERE " + " AND ".join(w)) if w else ""
    order_by = {"days": "a.days DESC", "level": "CASE a.level WHEN '紧急' THEN 0 WHEN '高' THEN 1 ELSE 2 END, a.days DESC",
                "order": "a.order_no"}.get(sort, "a.days DESC")
    off = (page - 1) * size
    base = ("FROM alerts a LEFT JOIN alert_state s "
            "ON s.order_no=a.order_no AND s.rule=a.rule")
    with core.ops() as o:
        total = o.execute(f"SELECT count(*) {base}{where}", a).fetchone()[0]
        rows = [dict(r) for r in o.execute(
            f"SELECT a.*, coalesce(s.status,'open') state, s.assignee, s.updated_at handle_at "
            f"{base}{where} ORDER BY {order_by} LIMIT ? OFFSET ?", a + [size, off]).fetchall()]
        summ = [dict(r) for r in o.execute(
            f"SELECT a.rule, a.rule_cn, a.level, count(*) n {base}{where} GROUP BY 1,2,3 ORDER BY n DESC",
            a).fetchall()]
    return {"total": total, "page": page, "size": size, "rows": rows, "summary": summ}


class ActionIn(BaseModel):
    order_no: str
    rule: str
    action: str          # claim / resolve / archive / reopen / note
    note: str = ""
    assignee: str = ""


@router.post("/alerts/action")
def alert_action(body: ActionIn, request: Request):
    u = core.require_editor(request)
    allowed = {"claim", "resolve", "archive", "reopen", "note", "handling"}
    if body.action not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的动作: {body.action}")

    status_map = {"claim": "claimed", "resolve": "resolved", "archive": "archived",
                  "reopen": "open", "handling": "handling"}
    new_status = status_map.get(body.action)
    assignee = body.assignee or (u["username"] if body.action in ("claim", "handling") else None)

    with core.ops() as o:
        cur = o.execute("SELECT status, note FROM alert_state WHERE order_no=? AND rule=?",
                        (body.order_no, body.rule)).fetchone()
        if not cur:
            o.execute("INSERT INTO alert_state (order_no,rule,status,assignee,note,updated_at,updated_by) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (body.order_no, body.rule, new_status or "open", assignee,
                       body.note, core.now_iso(), u["username"]))
        else:
            o.execute("""
                UPDATE alert_state SET
                  status = coalesce(?, status),
                  assignee = coalesce(?, assignee),
                  note = CASE WHEN ? <> '' THEN ? ELSE note END,
                  updated_at = ?, updated_by = ?
                WHERE order_no=? AND rule=?
            """, (new_status, assignee, body.note, body.note,
                  core.now_iso(), u["username"], body.order_no, body.rule))

    core.audit(u["username"], f"alert_{body.action}", f"{body.order_no}/{body.rule}",
               {"note": body.note})
    return {"ok": True, "status": new_status, "assignee": assignee}


@router.post("/orders/{order_no}/notes")
def add_note(order_no: str, body: dict, request: Request):
    u = core.require_editor(request)
    text = (body or {}).get("body", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="备注不能为空")
    with core.ops() as o:
        o.execute("INSERT INTO order_notes (order_no, ts, actor, body) VALUES (?,?,?,?)",
                  (order_no, core.now_iso(), u["username"], text[:2000]))
    core.audit(u["username"], "note", order_no, {"body": text[:200]})
    return {"ok": True}


# ---------------------------------------------------------------- 统计

@router.get("/stats")
def stats(request: Request, month: str = "", wh: str = "", pf: str = "",
          cr: str = "", cn: str = "", status: str = ""):
    core.require_user(request)
    where, args = _order_filters(month, wh, pf, cr, cn, status, "", "", "")
    con = core.duck()
    try:
        r = con.execute(f"""
            SELECT count(*) n,
                   coalesce(sum(件数),0) items,
                   sum(已签收) sign,
                   avg(仓内分钟) FILTER (WHERE 仓内分钟 > 0) hm,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0) tm,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 4320) s3,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 7200) s5,
                   count(*) FILTER (WHERE 物流分钟 > 0 AND 物流分钟 <= 10080) s7,
                   count(*) FILTER (WHERE 物流分钟 > 0) sg
            FROM fct_order{where}
        """, args).fetchone()
        months = con.execute(f"""
            SELECT 创建月份 m, count(*) n, sum(已签收) s,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 d
            FROM fct_order{where} GROUP BY 1 ORDER BY 1
        """, args).df().to_dict("records")
        by_carrier = con.execute(f"""
            SELECT 物流商 k, count(*) n, sum(已签收) s,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 d
            FROM fct_order{where} GROUP BY 1 ORDER BY n DESC LIMIT 20
        """, args).df().to_dict("records")
        by_wh = con.execute(f"""
            SELECT 仓库 k, count(*) n, sum(已签收) s,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 d
            FROM fct_order{where} GROUP BY 1 ORDER BY n DESC
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


@router.get("/health")
def health(request: Request):
    with core.ops() as o:
        a = o.execute("SELECT count(*) FROM alerts").fetchone()[0]
        last = o.execute("SELECT job, started_at, status FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    con = core.duck()
    try:
        orders = con.execute("SELECT count(*) FROM fct_order").fetchone()[0]
        rng = con.execute("SELECT min(创建日期), max(创建日期) FROM fct_order").fetchone()
    finally:
        con.close()
    return {"orders": orders, "alerts": a,
            "range": {"start": str(rng[0]), "end": str(rng[1])},
            "lastJob": dict(last) if last else None}
