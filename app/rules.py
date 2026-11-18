"""
规则中心：业务预警规则 + 渠道 SLA 的唯一来源。

页面（数据与规则）展示的文案、alerts.py 的判定口径、API 的筛选选项，
全部从这里取，避免"文档写一套、代码跑另一套"。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 预警规则

LEVEL_ORDER = {"紧急": 0, "高": 1, "中": 2}

# 判定优先级（数字越小越优先，一单多命中时取最小者为主预警）
PRIORITY = {
    "signout_timeout": 1,
    "returning": 2,
    "carrier_exception": 3,
    "delivery_failure": 4,
    "customs_hold": 5,
    "no_update": 6,
    "stagnation": 7,
    "not_online": 8,
    "transport_timeout": 9,
}

RULES = [
    {
        "key": "signout_timeout",
        "name": "超时未签出",
        "category": "履约准备异常",
        "level": "高",
        "scope": "全部团队 · 待签出履约单",
        "trigger": "履约单创建后超过 24 小时仍未完成仓库签出",
        "basis": "ERP 创建时间 · 签出时间 · 出库单状态",
        "exclude": "已取消、人工冻结的履约单",
        "recovery": "ERP 回传签出时间后自动恢复",
        "enabled": True,
    },
    {
        "key": "not_online",
        "name": "物流未上网",
        "category": "未上网异常",
        "level": "高",
        "scope": "全部团队 · 全部渠道",
        "trigger": "已签出超过 2 个自然日，17TRACK 仍停留在「仅收到信息」或「查无此单」",
        "basis": "签出时间 · 17TRACK 主状态 ∈ {InfoReceived, NotFound}",
        "exclude": "签出时间缺失；**完全没有轨迹数据的单不报**（无法区分「没上网」与「单号未同步进 17TRACK」，属数据缺口）",
        "recovery": "出现 InTransit_PickedUp 等揽收轨迹后自动恢复",
        "enabled": True,
    },
    {
        "key": "transport_timeout",
        "name": "运输超时",
        "category": "运输异常",
        "level": "高",
        "scope": "全部团队 · 按渠道 × 国家 SLA",
        "trigger": "已签出未签收，且已过天数 > 该线路历史 P90 妥投天数 + 2 个工作日",
        "basis": "签出时间 · 仓库×物流商×目的国 历史已签收单的 P90 妥投天数",
        "exclude": "已签收；17TRACK 已显示 Delivered；取消单",
        "recovery": "签收或人工归档后退出待办",
        "enabled": True,
    },
    {
        "key": "no_update",
        "name": "物流断更",
        "category": "运输异常",
        "level": "高",
        "scope": "全部团队 · 运输中包裹",
        "trigger": "最后一条有效轨迹后超过 3 个工作日完全没有新轨迹",
        "basis": "17TRACK 最新事件时间 · 工作日历",
        "exclude": "派送失败、等待自提、同步失败、已签收",
        "recovery": "出现任一新的有效轨迹后自动恢复",
        "enabled": True,
    },
    {
        "key": "stagnation",
        "name": "物流停滞",
        "category": "运输异常",
        "level": "中",
        "scope": "全部团队 · 运输中包裹",
        "trigger": "期间仍有轨迹更新，但连续停留在同一节点超过 3 个工作日",
        "basis": "17TRACK 子状态 InTransit_Arrival · 停滞天数",
        "exclude": "海关节点（归入海关卡关）、同步失败",
        "recovery": "标准化地点或处理节点发生变化",
        "enabled": True,
    },
    {
        "key": "customs_hold",
        "name": "海关卡关",
        "category": "海关卡关",
        "level": "高",
        "scope": "跨境渠道",
        "trigger": "海关节点停留超过 3 个工作日，或 17TRACK 提示清关需补充资料",
        "basis": "17TRACK 子状态 ∈ {InTransit_CustomsProcessing, InTransit_CustomsRequiringInformation}",
        "exclude": "已放行、已离开海关节点",
        "recovery": "资料补充完成或清关放行",
        "enabled": True,
    },
    {
        "key": "delivery_failure",
        "name": "派送异常",
        "category": "派送异常",
        "level": "紧急",
        "scope": "全部末端派送渠道",
        "trigger": "派送失败、等待自提，或收件人拒收",
        "basis": "17TRACK 主状态 ∈ {DeliveryFailure, AvailableForPickup} 或子状态 Exception_Rejected",
        "exclude": "已签收",
        "recovery": "重新派送、客户自提、签收或人工解决",
        "enabled": True,
    },
    {
        "key": "returning",
        "name": "包裹退运",
        "category": "包裹退运",
        "level": "紧急",
        "scope": "全部团队 · 全部渠道",
        "trigger": "17TRACK 识别包裹退运（退件中 / 已退回）",
        "basis": "17TRACK 子状态 ∈ {Exception_Returning, Exception_Returned}",
        "exclude": "无",
        "recovery": "退运完成或人工归档",
        "enabled": True,
    },
    {
        "key": "carrier_exception",
        "name": "其他异常",
        "category": "其他异常",
        "level": "紧急",
        "scope": "全部团队 · 全部渠道",
        "trigger": "17TRACK 异常无法归入运输、卡关、派送或退运等明确规则（丢件/破损/销毁/安全/取消）",
        "basis": "17TRACK 主状态 Exception 且子状态不在已明确分类内",
        "exclude": "延误、退运、拒收、派送失败、清关补资料",
        "recovery": "恢复运输、签收或人工确认关闭",
        "enabled": True,
    },
]

RULES_BY_KEY = {r["key"]: r for r in RULES}

# 明确不做（缺数据源），页面上也要写出来，避免被当成遗漏
NOT_IMPLEMENTED = [
    {"name": "履约单报错", "need": "ERP 建单/取号错误码"},
    {"name": "商品缺货", "need": "ERP 库存分配结果、缺货 SKU"},
    {"name": "拆单异常", "need": "ERP 订单分配物流渠道结果"},
]

# ---------------------------------------------------------------- 渠道 SLA

# 默认承诺时效（签出 → 签收，自然日）
DEFAULT_SLA_DAYS = 6
DEFAULT_TARGETS = {"d3": 40.0, "d5": 80.0, "d6": 90.0}

# 已知渠道的承诺时效（天）。未列出的走 DEFAULT_SLA_DAYS。
CHANNEL_SLA = {
    "WYT-USPS": 6,
    "Hete-Logistics": 6,
    "Yun Express": 8,
    "SF Express": 5,
    "DHL": 6,
    "UPS": 5,
    "YFH-Logistics": 6,
    "XS_Express": 10,
    "pep": 6,
    "postnet": 12,
    "BR-JZlogistic": 10,
    "Frodio-JYOFFline": 8,
}

# 各渠道的时效考核指标（达标率目标，百分比）
CHANNEL_TARGETS = {
    "WYT-USPS": {"d3": 45.0, "d5": 82.0, "d6": 92.0},
    "Hete-Logistics": {"d3": 40.0, "d5": 80.0, "d6": 90.0},
    "Yun Express": {"d3": 25.0, "d5": 60.0, "d6": 75.0},
    "SF Express": {"d3": 55.0, "d5": 88.0, "d6": 94.0},
    "DHL": {"d3": 40.0, "d5": 78.0, "d6": 88.0},
    "UPS": {"d3": 55.0, "d5": 90.0, "d6": 95.0},
    "postnet": {"d3": 10.0, "d5": 35.0, "d6": 55.0},
}

# 工作日判定（与 alerts.py 保持一致）
WORKDAY_RULE = "周一至周五计为工作日；法定节假日未剔除（如需精确请接入节假日日历）"


def sla_days(channel: str) -> int:
    return CHANNEL_SLA.get(channel, DEFAULT_SLA_DAYS)


def targets(channel: str) -> dict:
    return CHANNEL_TARGETS.get(channel, DEFAULT_TARGETS)


def public_rules():
    """给前端用的规则列表（按优先级排序）"""
    return sorted(RULES, key=lambda r: PRIORITY.get(r["key"], 99))
