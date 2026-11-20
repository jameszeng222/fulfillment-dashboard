"""Match complete order references and select fields for anonymous lookup."""
import re
from unicodedata import normalize

from track17 import OFFICIAL_STATUS, STATUS_CN

from . import core, tracking_archive
from .waybill import normalized as waybill_number


def lookup(query):
    value = normalize("NFKC", query).strip().upper()
    if not re.fullmatch(r"[A-Z0-9#_.+ /-]{4,128}", value):
        return None
    number = waybill_number(value)
    if len(number) < 4:
        return None
    fields = ("履约单号", "快递单号", "订单号", "平台订单号")
    matches = [f'upper(trim(o."{field}")) = ?' for field in fields]
    matches.append("o.单号归一 = ?")
    return " AND (" + " OR ".join(matches) + ")", [value] * len(fields) + [number]


TRACK_FIELDS = (
    "履约单号", "快递单号", "当前渠道", "物流商", "C端承运商", "仓库", "目的国中文", "平台",
    "轨迹状态", "轨迹状态中文", "轨迹子状态", "未注册", "回补状态", "已签收",
    "签出时间", "最新事件时间", "最新事件当地时间", "逐条事件数",
)
DETAIL_FIELDS = TRACK_FIELDS + (
    "单号归一", "目的国", "状态", "创建时间", "支付时间", "打包时间", "分配时间",
    "签收当地时间", "承运商签收时间", "查询时间", "妥投统计来源",
)
SUBSTATUS_CN = {sub: label for _, sub, _, label in OFFICIAL_STATUS}
EVENT_FIELDS = ("事件时间", "当地时间", "事件承运商")
MILESTONE_NAMES = {
    "InfoReceived", "PickedUp", "Departure", "Arrival", "AvailableForPickup",
    "OutForDelivery", "Delivered", "Returning", "Returned",
}


def select(row, fields):
    return {name: row[name] for name in fields if name in row}


def event(row):
    status = row.get("主状态")
    substatus = row.get("子状态")
    label = SUBSTATUS_CN.get(substatus) or STATUS_CN.get(status) or "承运商更新"
    return {**select(row, EVENT_FIELDS), "描述": label}


def matching_orders(con, match):
    condition, params = match
    active = core.active_facts(core.order_facts(con))
    return f"(SELECT * FROM {active} o WHERE TRUE{condition})", params


def tracking_projection(con, facts):
    tracking, missing, archive = tracking_archive.sources(con)
    events = core.columns(con, "trk_event")
    same_carrier = " AND (e.运输商代码=t.carrier OR e.运输商代码 IS NULL)" if "运输商代码" in events else ""
    event_count = f"(SELECT count(*) FROM trk_event e WHERE e.单号=o.单号归一{same_carrier})" if events else "0"
    return f"""
        SELECT o.*, t.状态 AS 轨迹状态, t.状态中文 AS 轨迹状态中文, t.子状态 AS 轨迹子状态,
               t.状态中文 AS 最新描述, t.最新事件时间, t.最新事件当地时间,
               coalesce(t.查询时间,h.查询时间) AS 查询时间, h.回补状态,
               t.carrier AS 运输商代码, h.carrier AS 回补运输商代码,
               (t.单号 IS NULL AND m.单号 IS NOT NULL) AS 未注册,
               coalesce(nullif(h.事件数,0),{event_count}) AS 逐条事件数,
               t.签收时间 AS tracked_delivery, t.签收当地时间 AS tracked_delivery_local
        FROM {facts} o LEFT JOIN {tracking} t ON t.单号=o.单号归一
        LEFT JOIN {missing} m ON m.单号=o.单号归一
        LEFT JOIN {archive} h ON h.履约单号=o.履约单号 AND h.单号=o.单号归一
    """


def search(query, page, size):
    result = {"total": 0, "page": page, "size": size, "rows": [], "statusSummary": [], "ruleSummary": []}
    match = lookup(query)
    if not match:
        return result
    with core.duck() as con:
        facts, params = matching_orders(con, match)
        result["total"] = con.execute(f"SELECT count(*) FROM {facts}", params).fetchone()[0]
        if result["total"]:
            sql = tracking_projection(con, facts)
            rows = con.execute(f"""
                SELECT * FROM ({sql}) ORDER BY 签出时间 DESC NULLS LAST, 履约单号
                LIMIT ? OFFSET ?
            """, [*params, size, (page - 1) * size]).df().to_dict("records")
            tracking_archive.fill_local_times(con, rows)
            result["rows"] = [select(row, TRACK_FIELDS + ("最新描述",)) for row in rows]
    return core.clean(result)
