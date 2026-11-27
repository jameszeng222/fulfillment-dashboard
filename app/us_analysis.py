"""Volume and milestone timing by destination and fulfillment channel."""
import csv
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pandas as pd

from . import core, analytics_snapshot
from .carrier_delivery import DELIVERED_BJ, DELIVERY_SOURCE, elapsed_minutes, reporting_facts
from .order_time import delivery_facts

ChannelDimension = Literal["lastmile", "current", "group"]
TransitBasis = Literal["paid", "created", "shipped", "paid_create", "create_ship", "ship_pickup", "pickup_delivered"]
Market = Literal["us", "other"]
PlaceLevel = Literal["all", "states", "regions"]
REFERENCE = Path(__file__).resolve().parents[1] / "reference"
UNKNOWN = "未识别州"
US_COUNTRIES = "('united states', 'united states of america', 'us', 'usa')"
SEGMENTS = {
    "paid_create": ("o.支付时间", "o.创建时间"),
    "create_ship": ("o.创建时间", "o.签出时间"),
    "ship_pickup": ("o.签出时间", "o.regional_online"),
    "pickup_delivered": ("o.regional_online", f"o.{DELIVERED_BJ}"),
    "shipped": ("o.签出时间", f"o.{DELIVERED_BJ}"),
    "created": ("o.创建时间", f"o.{DELIVERED_BJ}"),
    "paid": ("o.支付时间", f"o.{DELIVERED_BJ}"),
}
WORLD_ALIASES = {
    "South Korea": "Korea", "Congo, The Democratic Republic Of The": "Dem. Rep. Congo",
    "Czech Republic": "Czech Rep.", "Tanzania, United Republic Of": "Tanzania",
    "Côte d’Ivoire": "Côte d'Ivoire", "Aland Islands": "Aland", "North Macedonia": "Macedonia",
    "Bosnia And Herzegovina": "Bosnia and Herz.", "Equatorial Guinea": "Eq. Guinea",
    "South Sudan": "S. Sudan", "Eswatini": "Swaziland", "Cayman Islands": "Cayman Is.",
    "VIRGIN ISLAND (US)": "U.S. Virgin Is.", "Virgin Islands, U.S.": "U.S. Virgin Is.",
    "Northern Mariana Islands": "N. Mariana Is.",
}


def _normalize(value):
    return re.sub(r"[\s.]+", "", value.strip().lower())


@lru_cache(maxsize=1)
def _state_reference():
    # The seven core regions follow the 7-day Winit workbook. AK and HI
    # supplement its continental sample and territories are shown separately.
    with (REFERENCE / "us-fulfillment-states.csv").open(encoding="utf-8", newline="") as file:
        states = {row["code"]: row for row in csv.DictReader(file)}
    aliases = {_normalize(name): code for code, row in states.items()
               for name in (code, row["name"], row["label"])}
    aliases.update({"washingtondc": "DC", "virginislands": "VI", "usvirginislands": "VI",
                    "districtcolumbia": "DC", "tex": "TX", "德克萨斯": "TX"})
    return states, aliases


def _register_reference(con):
    states, aliases = _state_reference()
    con.register("us_state_names", pd.DataFrame(
        [(code, row["region"]) for code, row in states.items()], columns=["code", "region"]))
    con.register("us_state_aliases", pd.DataFrame(list(aliases.items()), columns=["alias", "code"]))
    return states


def _address_sql(con):
    columns = core.columns(con, "履约单明细")
    state = '''regexp_replace(lower(trim(d."省、州")), '[[:space:].]+', '', 'g')''' if "省、州" in columns else "NULL"
    postal = """regexp_extract(trim(d.邮编, ' "' || chr(39)), '^([0-9]{5})(?:-?[0-9]{4})?$', 1)""" \
        if "邮编" in columns else "NULL"
    return state, postal


def geography_sql(con, orders):
    _register_reference(con)
    state_field, postal_field = _address_sql(con)
    sql = f"""WITH zipcodes AS (
            SELECT postal_code,min(region_code) AS code FROM read_csv(?, all_varchar=true)
            WHERE region_code IN (SELECT code FROM us_state_names)
            GROUP BY postal_code HAVING count(DISTINCT region_code)=1
        ), address AS (
            SELECT d.履约单号,
               CASE WHEN count(DISTINCT s.code)=1 THEN min(s.code)
                    WHEN count(DISTINCT s.code)=0 AND count(DISTINCT z.code)=1 THEN min(z.code) END AS code
            FROM 履约单明细 d SEMI JOIN {orders} u USING (履约单号)
            LEFT JOIN us_state_aliases s ON s.alias={state_field}
            LEFT JOIN zipcodes z ON z.postal_code={postal_field}
            GROUP BY d.履约单号
        ) SELECT o.履约单号,coalesce(a.code,'') AS geo_state,coalesce(s.region,?) AS geo_region
          FROM {orders} o LEFT JOIN address a USING (履约单号)
          LEFT JOIN us_state_names s ON s.code=a.code"""
    return sql, [str(REFERENCE / "us-postal-timezones.csv"), UNKNOWN]


def _build_scope(con, market, dimension, cond, args, settings, level="all", place="", focus=""):
    stored = analytics_snapshot.available(con)
    orders = analytics_snapshot.TABLE if stored else delivery_facts(con, core.active_facts(core.order_facts(con)))
    country_filter = f"lower(trim(o.目的国)) {'IN' if market == 'us' else 'NOT IN'} {US_COUNTRIES}"
    group_join = ""
    if dimension == "group":
        group_rows = [(key, group["name"]) for group in json.loads(settings) for key in group["channels"]]
        con.register("us_channel_groups", pd.DataFrame(group_rows, columns=["channel", "label"]).astype("string"))
        group_join = "LEFT JOIN us_channel_groups g ON g.channel=trim(o.当前渠道)"
        channel = "coalesce(g.label, '未归组')"
    elif dimension == "current":
        channel = "coalesce(nullif(trim(o.当前渠道),''), '未填写当前渠道')"
    else:
        channel = "coalesce(nullif(trim(o.C端承运商),''), '未填写 C 端渠道')"
    params = list(args) if stored else []
    if stored:
        if market == "us":
            states, _ = _state_reference()
            zones = [(code, state["region"]) for code, state in states.items()]
            con.register("regional_state_zones", pd.DataFrame(zones, columns=["code", "region"]))
            geography_join = "LEFT JOIN regional_state_zones z ON z.code=o.analytics_state"
            state, region = "o.analytics_state", "coalesce(z.region,o.analytics_region)"
        else:
            geography_join = ""
            state, region = "o.目的国", "o.目的国"
        scope = f"""SELECT o.履约单号,o.单号归一,o.目的国,o.目的国中文,o.支付时间,o.创建时间,o.签出时间,
                         o.{DELIVERED_BJ},o.{DELIVERY_SOURCE},o.analytics_online,
                         {state} AS geo_state,{region} AS geo_region,{channel} AS geo_channel
                    FROM {orders} o {group_join} {geography_join}
                    WHERE {country_filter} AND nullif(trim(o.目的国),'') IS NOT NULL {cond}"""
    else:
        con.execute(f"""CREATE TEMP TABLE regional_orders AS SELECT o.* FROM {orders} o
            WHERE {country_filter} AND nullif(trim(o.目的国),'') IS NOT NULL {cond}""", args)
        if market == "us":
            geo, params = geography_sql(con, "regional_orders")
            scope = f"""SELECT o.*,g.geo_state,g.geo_region,{channel} AS geo_channel FROM regional_orders o
                LEFT JOIN ({geo}) g USING (履约单号) {group_join}"""
        else:
            scope = f"""SELECT o.*, o.目的国 AS geo_state, o.目的国 AS geo_region, {channel} AS geo_channel
                        FROM regional_orders o {group_join}"""
    clauses = []
    if level != "all":
        clauses.append("geo_region=?" if level == "regions" else "geo_state=?")
        params.append(place)
    if focus:
        clauses.append("geo_channel=?")
        params.append(focus)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    con.execute(f"CREATE TEMP TABLE regional_scope AS SELECT * FROM ({scope}) geography{where}", params)
    if market == "us":
        return _state_reference()[0]
    countries = con.execute("""
        SELECT 目的国,coalesce(max(nullif(目的国中文,'')),目的国) FROM regional_scope GROUP BY 1
    """).fetchall()
    return {country: {"code": country, "name": WORLD_ALIASES.get(country, country),
                      "label": "北马其顿" if country == "North Macedonia" else label, "region": "其他国家"}
            for country, label in countries}


def _metric_sources(con, keys, archive_version):
    delivery = any(SEGMENTS[key][1] == f"o.{DELIVERED_BJ}" for key in keys)
    facts = "regional_scope"
    if delivery and not analytics_snapshot.available(con):
        facts = reporting_facts(con, facts, page_only=True)
    if any(key in ("ship_pickup", "pickup_delivered") for key in keys):
        join, start = analytics_snapshot.scan_source(con, archive_version)
        facts = f"(SELECT o.*, {start} AS regional_online FROM {facts} o {join})"
    return facts, f"o.{DELIVERY_SOURCE}" if delivery else "NULL"


@lru_cache(maxsize=16)
def report(_snapshot, date_basis, basis, dimension, cond, args, settings, market="us", archive_version=None):
    with core.duck() as con:
        states = _build_scope(con, market, dimension, cond, args, settings)
        facts, delivery_source = _metric_sources(con, [basis], archive_version)
        duration = elapsed_minutes(*SEGMENTS[basis])
        grouping = "((), (state), (region), (channel), (state,channel), (region,channel))"
        rows = con.execute(f"""
            WITH scoped AS (
                SELECT o.geo_state AS state,o.geo_region AS region,o.geo_channel AS channel,
                       {duration} AS minutes, {delivery_source} AS delivery_source
                FROM {facts} o
            )
            SELECT grouping_id(state,region,channel), state, region, channel, count(*),
                   count(*) FILTER (WHERE minutes>0), count(*) FILTER (WHERE minutes>0 AND delivery_source='WMS'),
                   avg(minutes) FILTER (WHERE minutes>0)/1440.0,
                   quantile_cont(minutes, [0.90, 0.95]) FILTER (WHERE minutes>0)
            FROM scoped
            GROUP BY GROUPING SETS {grouping}
            ORDER BY count(*) DESC, state NULLS LAST, region NULLS LAST, channel NULLS LAST
        """).fetchall()
    result = {"dateBasis": date_basis, "basis": basis, "dimension": dimension, "market": market, "national": {},
              "states": {}, "regions": {}, "channels": []}
    place_channels = {}
    for flags, state, region, channel, orders, samples, fallback, average, percentiles in rows:
        p90, p95 = (value / 1440.0 for value in percentiles) if percentiles else (None, None)
        metrics = {"orders": orders, "samples": samples, "fallback": fallback, "avg": average, "p90": p90, "p95": p95}
        if flags == 7:
            result["national"] = metrics
        elif flags == 6:
            result["channels"].append({"label": channel, **metrics})
        elif flags in (3, 5):
            key = ("states", state) if flags == 3 else ("regions", region)
            result[key[0]][key[1]] = metrics
        else:
            key = ("states", state) if flags == 2 else ("regions", region)
            place_channels.setdefault(key, []).append({"label": channel, **metrics})
    for (level, place), channels in place_channels.items():
        result[level][place]["channels"] = channels
    if market == "other":
        result["regions"] = {}
    result["geography"] = states
    return result


@lru_cache(maxsize=24)
def segments(_snapshot, market, date_basis, dimension, cond, args, settings, level, place, focus, archive_version=None):
    with core.duck() as con:
        _build_scope(con, market, dimension, cond, args, settings, level, place, focus)
        facts, _ = _metric_sources(con, SEGMENTS, archive_version)
        minutes = ", ".join(f"{elapsed_minutes(*endpoints)} AS {key}" for key, endpoints in SEGMENTS.items())
        metrics = ", ".join(
            f"count(*) FILTER (WHERE {key}>0),avg({key}) FILTER (WHERE {key}>0),"
            f"quantile_cont({key},0.9) FILTER (WHERE {key}>0)" for key in SEGMENTS)
        summary = con.execute(f"""
            WITH measured AS (
                SELECT {minutes} FROM {facts} o
            ) SELECT count(*), {metrics} FROM measured
        """).fetchone()
    records = []
    for index, key in enumerate(SEGMENTS):
        count, avg, p90 = summary[1 + index * 3:4 + index * 3]
        records.append({"key": key, "samples": count, "avgMinutes": avg, "p90Minutes": p90})
    return {"total": summary[0], "segments": records, "market": market, "dateBasis": date_basis}
