"""Match complete order references and select fields for anonymous lookup."""
import re
from unicodedata import normalize

from track17 import OFFICIAL_STATUS, STATUS_CN

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
