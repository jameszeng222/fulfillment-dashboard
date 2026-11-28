"""Configurable shipment event deadlines evaluated against the local 17TRACK history."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
import json
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints, model_validator

from . import alert_export, channel_groups, core, rules
from .tracking_archive import EVENT_TABLE, TABLE
from .tracking_clock import carrier_wall_time

router = APIRouter(prefix="/api")
UTC = timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")
DayOffset = Annotated[int, Field(ge=0, le=14)] | None
Schedule = Annotated[list[DayOffset], Field(min_length=7, max_length=7)]
Keywords = Annotated[list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]],
                     Field(min_length=1, max_length=12)]


class DispatchSite(BaseModel):
    name: str = Field(min_length=1, max_length=70)
    warehouses: list[str] = Field(default_factory=list, max_length=30)
    cutoff: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class Rule(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=70)
    enabled: bool = True
    scope: Literal["lastmile", "current", "group"]
    channels: Keywords
    carrier: int | None = Field(default=None, ge=1, le=999999999)
    anchor: Literal["shipped", "event"]
    start: list[str] = Field(default_factory=list, max_length=12)
    end: Keywords
    before: Schedule
    after: Schedule | None = None
    sites: list[DispatchSite] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def validate_schedule(self):
        if not self.name.strip():
            raise ValueError("请填写规则名称")
        if self.anchor == "event":
            if not self.start or any(not term.strip() or len(term) > 160 for term in self.start):
                raise ValueError("请填写起始轨迹关键词")
        elif not self.sites or self.after is None:
            raise ValueError("出库规则需要至少一个截单地点和截单后的排期")
        else:
            warehouses = [code.strip().upper() for site in self.sites for code in site.warehouses]
            if not all(warehouses) or len(warehouses) != len(set(warehouses)):
                raise ValueError("同一仓库只可配置一个截单地点")
            if len(self.sites) > 1 and any(not site.warehouses for site in self.sites):
                raise ValueError("多个地点需要分别填写仓库代码")
        return self


class Configuration(BaseModel):
    revision: int = Field(ge=0)
    rules: list[Rule] = Field(max_length=20)


DEFAULTS = Configuration(revision=0, rules=[
    Rule(key="3pe_export", name="出口延误", scope="lastmile", channels=["3PE Express"], carrier=191376,
         anchor="shipped", end=["Departed from origin airport"],
         before=[1, 1, 1, 1, 1, 2, 2], after=[2, 2, 2, 2, 3, 3, 2],
         sites=[DispatchSite(name="广州", warehouses=["JY01", "JY02"], cutoff="14:30"),
                DispatchSite(name="许昌", warehouses=["XC01", "XCWH01"], cutoff="16:30")],
         note="出库和承诺起飞日期均按北京时间。签出时刻等于截单时刻时，使用截单后排期。"),
    Rule(key="3pe_lastmile", name="尾程提取延误", scope="lastmile", channels=["3PE Express"], carrier=191376,
         anchor="event", start=["Arrived at destination airport"], end=["Departed from destination facility"],
         before=[None, 0, 0, 0, 3, 2, 1],
         note="按落地机场当地日期考核。3PE 的目的地设施离站扫描作为提取参考，周一落地暂不考核。"),
])


def configuration(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='fulfillment_settings'").fetchone():
        row = db.execute("SELECT payload FROM fulfillment_settings WHERE kind='track_rules' AND name='channels'").fetchone()
        if row:
            return Configuration.model_validate_json(row[0])
    return DEFAULTS


@router.get("/channel-track-rules")
def get_rules(request: Request):
    core.require_user(request)
    with core.ops() as db:
        return configuration(db)


@router.put("/channel-track-rules")
def put_rules(body: Configuration, request: Request):
    user = core.require_editor(request)
    keys = {item.key for item in body.rules}
    names = {item.name.strip().casefold() for item in body.rules}
    if len(keys) != len(body.rules) or len(names) != len(body.rules):
        raise HTTPException(422, "轨迹规则编号和名称需要唯一")
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        if configuration(db).revision != body.revision:
            raise HTTPException(409, "规则已更新，请刷新后再保存")
        result = body.model_copy(update={"revision": body.revision + 1})
        rules.save_setting(db, "track_rules", "channels", result.model_dump())
        core.audit(user["username"], "channel_track_rules", "channels", {"rules": list(keys)}, connection=db)
    return result


@dataclass(frozen=True)
class Event:
    at: datetime
    wall: str | None
    text: str
    location: str | None


# Recognized destination scan locations use their IANA zone across daylight saving changes.
HUBS = (
    ("America/New_York", ("jamaica", "jfk", "duluth", "upper marlboro", "atlanta", "new york", "miami", "newark")),
    ("America/Chicago", ("hillside", "missouri city", "chicago", "dallas", "houston", "memphis")),
    ("America/Los_Angeles", ("los angeles", "lax", "ontario ca", "san francisco", "seattle")),
)


def _wall_time(value):
    if not value or len(value) < 16:
        return None
    try:
        cleaned = re.sub(r"\s+([+-]\d{2}:?\d{2})$", r"\1", value.strip().replace("Z", "+00:00"))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _local_anchor(event):
    wall = _wall_time(event.wall)
    location = (event.location or "").casefold()
    for name, cities in HUBS:
        if any(re.search(rf"(?<!\w){city}(?!\w)", location) for city in cities):
            zone = ZoneInfo(name)
            candidate = event.at.astimezone(zone)
            if wall is None or candidate.date() == wall.date():
                if wall is None or wall.utcoffset() is None or candidate.utcoffset() == wall.utcoffset():
                    return candidate
    if wall is None or wall.utcoffset() is None:
        return None
    return wall


def _completion_instant(event, rule):
    if rule.anchor == "shipped" and rule.carrier == 191376:
        wall = _wall_time(event.wall)
        location = (event.location or "").casefold()
        if wall is not None and (wall.utcoffset() == timedelta(hours=8) or "china" in location or "中国" in location):
            return wall.replace(tzinfo=BEIJING).astimezone(UTC)
    return event.at


def outcome(rule, shipped, warehouse, events, checked, now):
    """Return late evidence, a missing-scan exception, or an unaudited deadline."""
    shipment = shipped.replace(tzinfo=BEIJING)
    start = shipment.astimezone(UTC)
    anchor, site_name, start_text = shipment, "", "签出"
    if rule.anchor == "shipped":
        site = next((site for site in rule.sites if not site.warehouses or warehouse.upper() in
                     {code.strip().upper() for code in site.warehouses}), None)
        if site is None:
            return None
        site_name = site.name
        schedule = rule.before if shipment.time() < time.fromisoformat(site.cutoff) else rule.after
    else:
        terms = [word.strip().casefold() for word in rule.start]
        found = next((event for event in events if event.at >= start and any(word in event.text for word in terms)), None)
        if found is None:
            return None
        anchor = _local_anchor(found)
        if anchor is None:
            return {"state": "unknown_clock"}
        start, start_text = found.at, found.text
        schedule = rule.before
    offset = schedule[anchor.weekday()]
    if offset is None:
        return None
    end_day = anchor.date() + timedelta(days=offset)
    deadline = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=anchor.tzinfo)
    if now < deadline:
        return None
    targets = [word.casefold() for word in rule.end]
    eligible = [(_completion_instant(event, rule), event) for event in events
                if any(word in event.text for word in targets)]
    actual = min(((instant, event) for instant, event in eligible if start <= instant <= now),
                 default=None, key=lambda pair: pair[0])
    if actual and actual[0] < deadline:
        return None
    if not actual and (checked is None or checked < deadline):
        return {"state": "unverified"}
    return {"state": "late" if actual else "missing", "site": site_name, "anchor_at": anchor.isoformat(),
            "anchor_text": start_text, "due": end_day.isoformat(), "deadline": deadline.isoformat(),
            "event_at": actual[0].astimezone(anchor.tzinfo).isoformat() if actual else None,
            "event_text": actual[1].text if actual else None, "checked_at": checked.isoformat() if checked else None}


def _scope(rule, groups):
    if rule.scope == "group":
        selection = channel_groups.filter_values(json.dumps(rule.channels, ensure_ascii=False), groups)
        return channel_groups.predicate("o.当前渠道", selection)
    column = "C端承运商" if rule.scope == "lastmile" else "当前渠道"
    names = [core.last_mile_name(name) if rule.scope == "lastmile" else name for name in rule.channels]
    return f"coalesce(nullif(lower(trim(o.{column})),''),'__empty__') IN (SELECT unnest(CAST(?::JSON AS VARCHAR[])))", [
        json.dumps([name.casefold() for name in names], ensure_ascii=False)]


def _events_and_checks(con, rule):
    events = defaultdict(list)
    checks = {}
    source, args = [], []
    terms = list(dict.fromkeys(word.casefold().strip() for word in rule.start + rule.end if word.strip()))
    for table, number, carrier, clock, wall, raw, iso, text, place in (
        ("trk_event", "单号", "运输商代码", "e.事件时间",
         "e.当地时间", "NULL", "NULL", "e.描述", "e.地点"),
        (EVENT_TABLE, "tracking_no", "carrier_code", "timezone('UTC',try_cast(e.event_time_utc AS TIMESTAMPTZ))",
         "NULL", "e.event_time_raw", "e.event_time_iso", "e.description", "e.location"),
    ):
        fields = core.columns(con, table)
        if not fields:
            continue
        if table == "trk_event" and "当地时间" not in fields:
            wall = "NULL"
        if table == EVENT_TABLE and "event_time_iso" not in fields:
            iso = "NULL"
        match = " AND e." + carrier + "=?" if rule.carrier else ""
        source.append(f"""
            SELECT e.{number} AS number, {clock} AS event_at, {wall} AS wall, {raw} AS raw, {iso} AS iso,
                   lower(trim({text})) AS description, {place} AS location
            FROM {table} e SEMI JOIN track_rule_orders o ON o.number=e.{number}
            WHERE {clock} IS NOT NULL {match}
              AND ({' OR '.join(f'contains(lower({text}),?)' for _ in terms)})
        """)
        args.extend(([rule.carrier] if rule.carrier else []) + terms)
    if source:
        for number, at, wall, raw, iso, desc, place in con.execute(
                " UNION ALL ".join(source) + " ORDER BY event_at, wall NULLS LAST", args).fetchall():
            local = wall or carrier_wall_time(raw, iso)
            events[number].append(Event(at.replace(tzinfo=UTC), local, desc, place))

    timestamps, args = [], []
    for table in ("trk_status", TABLE):
        if not core.columns(con, table):
            continue
        match = " AND t.carrier=?" if rule.carrier else ""
        timestamps.append(f"""SELECT t.单号 AS number, t.查询时间 AS checked
                           FROM {table} t SEMI JOIN track_rule_orders o ON o.number=t.单号
                           WHERE t.查询时间 IS NOT NULL {match}""")
        if rule.carrier:
            args.append(rule.carrier)
    if timestamps:
        checks = {number: checked.replace(tzinfo=BEIJING).astimezone(UTC) for number, checked in con.execute(
            f"SELECT number,max(checked) FROM ({' UNION ALL '.join(timestamps)}) GROUP BY number", args).fetchall()}
    return events, checks


@lru_cache(maxsize=6)
def _compute(_snapshot, payload, group_payload, start_day, asof):
    config = Configuration.model_validate_json(payload)
    groups = json.loads(group_payload)
    counts, rows = [], []
    with core.duck() as con:
        facts = core.active_facts(core.order_facts(con))
        for rule in config.rules:
            if not rule.enabled:
                continue
            scope, args = _scope(rule, groups)
            warehouses = sorted({code.upper().strip() for site in rule.sites for code in site.warehouses})
            location_filter = ""
            if rule.anchor == "shipped" and warehouses:
                location_filter = "AND upper(trim(o.仓库)) IN (SELECT unnest(CAST(?::JSON AS VARCHAR[])))"
                args.append(json.dumps(warehouses, ensure_ascii=False))
            con.execute(f"""
                CREATE OR REPLACE TEMP TABLE track_rule_orders AS
                SELECT o.履约单号 AS order_no, o.单号归一 AS number, o.快递单号 AS waybill, coalesce(o.仓库,'') AS warehouse,
                       o.当前渠道 AS channel, o.C端承运商 AS lastmile, o.签出时间 AS shipped_at
                FROM {facts} o
                WHERE o.签出时间>=? AND o.签出时间<=?
                  AND nullif(o.单号归一,'') IS NOT NULL AND ({scope or 'TRUE'}) {location_filter}
            """, [start_day, asof.astimezone(BEIJING).replace(tzinfo=None), *args])
            events, checks = _events_and_checks(con, rule)
            tally = {"rule": rule.key, "name": rule.name, "late": 0, "missing": 0, "unverified": 0, "unknown_clock": 0}
            columns = ("order_no", "number", "waybill", "warehouse", "channel", "lastmile", "shipped_at")
            for order in con.execute("SELECT " + ",".join(columns) + " FROM track_rule_orders").fetchall():
                item = dict(zip(columns, order))
                hit = outcome(rule, item["shipped_at"], item["warehouse"], events.get(item["number"], ()),
                              checks.get(item["number"]), asof)
                if hit is None:
                    continue
                tally[hit["state"]] += 1
                if hit["state"] in ("late", "missing"):
                    item["shipped_at"] = item["shipped_at"].isoformat(sep=" ", timespec="minutes")
                    rows.append({"rule": rule.key, "name": rule.name, **item, **hit})
            counts.append(tally)
    rows.sort(key=lambda item: (item["shipped_at"], item["order_no"], item["rule"]), reverse=True)
    return counts, rows


@router.get("/channel-track-exceptions")
def exceptions(request: Request, days: int = Query(30, ge=1, le=90), rule: str = "", q: str = "",
               state: Literal["", "late", "missing"] = "",
               page: int = Query(1, ge=1), size: int = Query(40, ge=1, le=100),
               export: Literal["all", "tracks"] | None = None):
    core.require_user(request)
    with core.ops() as db:
        settings = configuration(db)
        groups = channel_groups.load(db)["groups"]
    asof = datetime.now(UTC).replace(second=0, microsecond=0)
    start_day = asof.astimezone(BEIJING).date() - timedelta(days=days - 1)
    counts, rows = _compute(core.snapshot_key(), settings.model_dump_json(), json.dumps(groups), start_day, asof)
    key = q.strip().casefold()
    matches = [item for item in rows if (not rule or item["rule"] == rule) and (not state or item["state"] == state) and
               (not key or key in item["order_no"].casefold() or key in (item["waybill"] or "").casefold())]
    if export:
        if not matches:
            raise HTTPException(404, "当前没有可导出的特殊轨迹命中")
        headers = ["履约单号", "运单号", "命中规则", "异常类型", "仓库", "地点", "当前渠道", "C 端承运商",
                   "起始时间（当地）", "起始节点", "承诺截止（当地）", "实际节点时间", "实际节点", "最近核对（UTC）"]
        content = alert_export.csv_content(headers, (
            (row["order_no"], row["waybill"], row["name"], "节点超时" if row["state"] == "late" else "超时未见节点",
             row["warehouse"], row["site"], row["channel"], row["lastmile"], row["anchor_at"], row["anchor_text"],
             row["deadline"], row["event_at"], row["event_text"], row["checked_at"])
            for row in matches
        ))
        filename = "特殊轨迹监控-全部命中"
        if export == "all":
            return alert_export.download(content, filename)
        pairs = [{"order_no": row["order_no"], "tracking_no": row["waybill"]} for row in matches]
        return alert_export.download(content, filename + "-含完整轨迹", events=pairs, details="特殊轨迹明细.csv")
    offset = (page - 1) * size
    return {"rows": matches[offset:offset+size], "counts": counts, "total": len(matches),
            "page": page, "size": size, "from": start_day.isoformat(), "asof": asof.isoformat()}
