"""四个业务页面的数据接口：总览 / 轨迹监控 / 渠道时效 / 规则。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from . import core, rules

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- 公共

def _multi(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _cond(month="", wh="", pf="", cr="", cn="", status="", start="", end="", q="",
          alias="o"):
    """返回 (sql, args)，sql 形如 " AND ..."，可直接拼在 WHERE 1=1 之后。"""
    w, a = [], []

    def inl(col, vals):
        if vals:
            w.append(f'{alias}."{col}" IN ({",".join("?" * len(vals))})')
            a.extend(vals)

    if month:
        w.append(f"{alias}.创建月份 = ?"); a.append(month)
    inl("仓库", _multi(wh))
    inl("平台", _multi(pf))
    inl("物流商", _multi(cr))
    inl("目的国", _multi(cn))
    inl("状态", _multi(status))
    if start:
        w.append(f"{alias}.创建日期 >= ?"); a.append(start)
    if end:
        w.append(f"{alias}.创建日期 <= ?"); a.append(end)
    if q:
        w.append(f"({alias}.履约单号 LIKE ? OR {alias}.快递单号 LIKE ? OR {alias}.订单号 LIKE ?)")
        a.extend([f"%{q}%"] * 3)
    return (" AND " + " AND ".join(w)) if w else "", a


def _sla_values() -> str:
    """把渠道 SLA 目标转成 DuckDB 的 VALUES 表，供单次 join 用。"""
    rows = []
    for ch, days in rules.CHANNEL_SLA.items():
        t = rules.targets(ch)
        rows.append(f"('{ch}',{days},{t['d3']},{t['d5']},{t['d6']})")
    d = rules.DEFAULT_TARGETS
    rows.append(f"('__default__',{rules.DEFAULT_SLA_DAYS},{d['d3']},{d['d5']},{d['d6']})")
    return "VALUES " + ",".join(rows)


# ---------------------------------------------------------------- 总览

@router.get("/overview")
def overview(request: Request, month: str = "", wh: str = "", pf: str = "", cr: str = "",
             cn: str = "", start: str = "", end: str = ""):
    core.require_user(request)
    cond, args = _cond(month, wh, pf, cr, cn, start=start, end=end)

    con = core.duck()
    try:
        k = con.execute(f"""
            SELECT count(*) n, coalesce(sum(件数),0) items, sum(已签收) sign,
                   avg(仓内分钟) FILTER (WHERE 仓内分钟 > 0) hm,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0) tm,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0) sgn,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0 AND 物流分钟 <= 4320) d3,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0 AND 物流分钟 <= 7200) d5,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0 AND 物流分钟 <= 8640) d6,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0 AND 物流分钟 <= 10080) d7
            FROM fct_order o WHERE 1=1{cond}
        """, args).fetchone()

        def by(col, limit=20):
            return con.execute(f"""
                SELECT o."{col}" k, count(*) n, sum(o.已签收) s,
                       avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0)/1440.0 d
                FROM fct_order o WHERE 1=1{cond} GROUP BY 1 ORDER BY n DESC LIMIT {limit}
            """, args).df().to_dict("records")

        monthly = con.execute(f"""
            SELECT o.创建月份 m, count(*) n, sum(o.已签收) s,
                   avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0)/1440.0 d
            FROM fct_order o WHERE 1=1{cond} GROUP BY 1 ORDER BY 1
        """, args).df().to_dict("records")

        by_carrier = by("物流商", 20)
        by_wh = by("仓库", 20)
        by_country = by("目的国中文", 15)
        by_status = by("状态", 20)
        asof = con.execute("SELECT max(创建日期) FROM fct_order").fetchone()[0]
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
            "rate3": rate(k[6]), "rate5": rate(k[7]), "rate6": rate(k[8]), "rate7": rate(k[9]),
            "signed": sgn,
        },
        "monthly": monthly, "byCarrier": by_carrier, "byWarehouse": by_wh,
        "byCountry": by_country, "byStatus": by_status,
    })


# ---------------------------------------------------------------- 轨迹监控

@router.get("/track")
def track(request: Request,
          page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
          q: str = "", month: str = "", wh: str = "", pf: str = "", cr: str = "",
          cn: str = "", tstat: str = "", rule: str = "", signed: str = "",
          sort: str = "停滞天数", order: str = "desc"):
    core.require_user(request)
    cond, args = _cond(month, wh, pf, cr, cn)

    if tstat:
        vals = _multi(tstat)
        cond += f' AND t.状态 IN ({",".join("?" * len(vals))})'
        args.extend(vals)
    if rule:
        vals = _multi(rule)
        cond += f' AND a.主预警规则 IN ({",".join("?" * len(vals))})'
        args.extend(vals)
    if signed in ("0", "1"):
        cond += " AND o.已签收 = ?"; args.append(int(signed))

    sort_cols = {"停滞天数": "t.停滞天数", "签出时间": "o.签出时间", "创建时间": "o.创建时间",
                 "物流分钟": "o.物流分钟", "履约单号": "o.履约单号"}
    sc = sort_cols.get(sort, "t.停滞天数")
    direction = "ASC" if order.lower() == "asc" else "DESC"
    off = (page - 1) * size

    base = ("FROM fct_order o "
            "LEFT JOIN trk_status t ON t.单号 = o.单号归一 "
            "LEFT JOIN fct_order_alert a ON a.履约单号 = o.履约单号"
            " WHERE 1=1" + cond)

    con = core.duck()
    try:
        total = con.execute(f"SELECT count(*) {base}", args).fetchone()[0]
        rows = con.execute(f"""
            SELECT o.履约单号, o.快递单号, o.平台, o.仓库, o.物流商, o.C端承运商,
                   o.目的国中文, o.状态, o.创建时间, o.签出时间, o.签收时间,
                   o.物流分钟, o.已签收,
                   t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
                   t.最新事件时间, t.最新描述, t.地点, t.停滞天数,
                   a.主预警名称, a.主预警级别, a.预警数,
                   (o.单号归一 IN (SELECT 单号 FROM trk_missing)) AS 未注册
            {base}
            ORDER BY {sc} {direction} NULLS LAST, o.履约单号
            LIMIT ? OFFSET ?
        """, args + [size, off]).df()

        status_summary = con.execute(f"""
            SELECT coalesce(t.状态中文,'无轨迹') 状态, count(*) n {base}
            GROUP BY 1 ORDER BY n DESC
        """, args).df().to_dict("records")
        rule_summary = con.execute(f"""
            SELECT coalesce(a.主预警名称,'无异常') 异常, count(*) n {base}
            GROUP BY 1 ORDER BY n DESC
        """, args).df().to_dict("records")
    finally:
        con.close()

    return core.clean({"total": int(total), "page": page, "size": size,
                       "rows": rows.to_dict("records"),
                       "statusSummary": status_summary, "ruleSummary": rule_summary})


# ---------------------------------------------------------------- 渠道时效

@router.get("/channels")
def channels(request: Request, month: str = "", wh: str = "", pf: str = "",
             cn: str = "", min_n: int = 100):
    core.require_user(request)
    cond, args = _cond(month, wh, pf, "", cn)
    con = core.duck()
    try:
        asof = con.execute("SELECT max(创建日期) FROM fct_order").fetchone()[0]
        df = con.execute(f"""
            WITH tgt(ch, target, d3t, d5t, d6t) AS ({_sla_values()})
            SELECT coalesce(o.物流商,'(未分配)') ch,
                   count(*) n,
                   count(*) FILTER (WHERE o.已签收=1) signed,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0) sgn,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0 AND o.物流分钟 <= 4320) d3,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0 AND o.物流分钟 <= 7200) d5,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0 AND o.物流分钟 <= 8640) d6,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0 AND o.物流分钟 <= 10080) d7,
                   avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0) tm,
                   quantile_cont(o.物流分钟, 0.9) FILTER (WHERE o.物流分钟 > 0) p90,
                   count(*) FILTER (WHERE o.已签收=0 AND o.签出时间 IS NOT NULL) in_transit,
                   count(*) FILTER (WHERE o.已签收=0 AND o.签出时间 IS NOT NULL
                         AND date_diff('day', o.签出时间::DATE, DATE '{asof}')
                             > coalesce(tgt.target, {rules.DEFAULT_SLA_DAYS})) overdue
            FROM fct_order o
            LEFT JOIN tgt ON tgt.ch = o.物流商
            WHERE 1=1{cond}
            GROUP BY 1 HAVING count(*) >= {int(min_n)}
            ORDER BY n DESC
        """, args).df()

        rows = []
        for _, r in df.iterrows():
            ch = r["ch"]
            target = rules.sla_days(ch)
            tgt = rules.targets(ch)
            sgn = int(r["sgn"] or 0)

            def pct(x):
                return round(100.0 * int(x) / sgn, 1) if sgn else None

            r6 = pct(r["d6"])
            avg_days = round(float(r["tm"]) / 1440.0, 2) if r["tm"] else None
            in_tr = int(r["in_transit"] or 0)
            over = int(r["overdue"] or 0)
            rows.append({
                "channel": ch, "orders": int(r["n"]), "signed": int(r["signed"] or 0),
                "avgDays": avg_days,
                "p90Days": round(float(r["p90"]) / 1440.0, 2) if r["p90"] else None,
                "rate3": pct(r["d3"]), "rate5": pct(r["d5"]),
                "rate6": r6, "rate7": pct(r["d7"]),
                "target": target, "targets": tgt,
                "inTransit": in_tr, "overdue": over,
                "overRate": round(100.0 * over / in_tr, 1) if in_tr else None,
                # 达标判定：6 天达成率达到该渠道目标，且平均时效不超过承诺
                "rate6Ok": (r6 is not None and r6 >= tgt["d6"]),
                "avgOk": (avg_days <= target) if avg_days is not None else None,
                "verdict": ("达标" if (r6 is not None and r6 >= tgt["d6"] and avg_days and avg_days <= target)
                            else "未达标"),
            })

        by_country = con.execute(f"""
            SELECT o.物流商 ch, coalesce(o.目的国中文, o.目的国) ctry, count(*) n,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0 AND o.物流分钟 <= 8640) d6,
                   count(*) FILTER (WHERE o.已签收=1 AND o.物流分钟 > 0) sgn,
                   avg(o.物流分钟) FILTER (WHERE o.物流分钟 > 0)/1440.0 avgd
            FROM fct_order o WHERE 1=1{cond}
            GROUP BY 1,2 HAVING count(*) >= 200
            ORDER BY n DESC LIMIT 40
        """, args).df().to_dict("records")
    finally:
        con.close()

    for r in by_country:
        r["rate6"] = round(100.0 * r["d6"] / r["sgn"], 1) if r["sgn"] else None
        r["avgDays"] = round(float(r["avgd"]), 2) if r["avgd"] else None

    return core.clean({"asof": str(asof), "rows": rows, "byCountry": by_country})


# ---------------------------------------------------------------- 规则与 SLA

@router.get("/rules")
def rules_api(request: Request):
    core.require_user(request)
    con = core.duck()
    try:
        asof = con.execute("SELECT max(创建日期) FROM fct_order").fetchone()[0]
        perf = con.execute("""
            SELECT coalesce(物流商,'(未分配)') ch, count(*) n,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0) sgn,
                   count(*) FILTER (WHERE 已签收=1 AND 物流分钟 > 0 AND 物流分钟 <= 8640) d6,
                   avg(物流分钟) FILTER (WHERE 物流分钟 > 0)/1440.0 avgd
            FROM fct_order GROUP BY 1 ORDER BY n DESC
        """).df().to_dict("records")
        rule_hits = con.execute("""
            SELECT 规则 k, count(*) n, round(avg(天数),1) avg_days
            FROM fct_alert WHERE 是否主预警 GROUP BY 1 ORDER BY n DESC
        """).df().to_dict("records")
    finally:
        con.close()

    for r in perf:
        r["target"] = rules.sla_days(r["ch"])
        r["targets"] = rules.targets(r["ch"])
        r["rate6"] = round(100.0 * r["d6"] / r["sgn"], 1) if r["sgn"] else None
        r["avgDays"] = round(float(r["avgd"]), 2) if r["avgd"] else None

    hits = {h["k"]: h for h in rule_hits}
    for r in rules.public_rules():
        h = hits.get(r["key"], {})
        r["hits"] = h.get("n")
        r["avgDays"] = h.get("avg_days")

    return core.clean({
        "asof": str(asof),
        "rules": rules.public_rules(),
        "notImplemented": rules.NOT_IMPLEMENTED,
        "workdayRule": rules.WORKDAY_RULE,
        "slaDefault": rules.DEFAULT_SLA_DAYS,
        "channels": perf,
    })
