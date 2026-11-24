"""Volume and delivery timing by US destination and fulfillment channel."""
import csv
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pandas as pd

from . import core
from .carrier_delivery import DELIVERED_BJ, DELIVERY_SOURCE, elapsed_minutes, reporting_facts
from .order_time import delivery_facts

ChannelDimension = Literal["lastmile", "current", "group"]
TransitBasis = Literal["paid", "shipped"]
REFERENCE = Path(__file__).resolve().parents[1] / "reference"
UNKNOWN = "未识别州"


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


def _scoped_delivery_facts(con):
    facts = reporting_facts(con, "us_orders", page_only=True)
    columns = core.columns(con, "履约单明细")
    state = '''regexp_replace(lower(trim(d."省、州")), '[[:space:].]+', '', 'g')''' if "省、州" in columns else "NULL"
    postal = """regexp_extract(trim(d.邮编, ' "' || chr(39)), '^([0-9]{5})(?:-?[0-9]{4})?$', 1)""" \
        if "邮编" in columns else "NULL"
    return facts, state, postal


@lru_cache(maxsize=16)
def report(_snapshot, date_basis, basis, dimension, cond, args, settings):
    with core.duck() as con:
        states = _register_reference(con)
        orders = delivery_facts(con, core.active_facts(core.order_facts(con)))
        con.execute(f"""
            CREATE TEMP TABLE us_orders AS SELECT o.* FROM {orders} o
            WHERE lower(trim(o.目的国)) IN ('united states', 'united states of america', 'us', 'usa')
              {cond}
        """, args)
        facts, state_field, postal_field = _scoped_delivery_facts(con)
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
        duration = "o.物流分钟" if basis == "shipped" else elapsed_minutes("o.支付时间", f"o.{DELIVERED_BJ}")
        rows = con.execute(f"""
            WITH zipcodes AS (
                SELECT postal_code,min(region_code) AS code FROM read_csv(?, all_varchar=true)
                WHERE region_code IN (SELECT code FROM us_state_names)
                GROUP BY postal_code HAVING count(DISTINCT region_code)=1
            ), address AS (
                SELECT d.履约单号,
                   CASE WHEN count(DISTINCT s.code)=1 THEN min(s.code)
                        WHEN count(DISTINCT s.code)=0 AND count(DISTINCT z.code)=1 THEN min(z.code) END AS code
                FROM 履约单明细 d SEMI JOIN us_orders u USING (履约单号)
                LEFT JOIN us_state_aliases s ON s.alias={state_field}
                LEFT JOIN zipcodes z ON z.postal_code={postal_field}
                GROUP BY d.履约单号
            ), scoped AS (
                SELECT coalesce(a.code,'') AS state, coalesce(s.region,?) AS region, {channel} AS channel,
                       {duration} AS minutes, o.{DELIVERY_SOURCE} AS delivery_source
                FROM {facts} o LEFT JOIN address a USING (履约单号)
                LEFT JOIN us_state_names s ON s.code=a.code {group_join}
            )
            SELECT grouping_id(state,region,channel), state, region, channel, count(*),
                   count(*) FILTER (WHERE minutes>0), count(*) FILTER (WHERE minutes>0 AND delivery_source='WMS'),
                   round(avg(minutes) FILTER (WHERE minutes>0)/1440.0, 2),
                   round(quantile_cont(minutes, 0.9) FILTER (WHERE minutes>0)/1440.0, 2)
            FROM scoped
            GROUP BY GROUPING SETS ((), (state), (region), (channel), (state,channel), (region,channel))
            ORDER BY count(*) DESC, state NULLS LAST, region NULLS LAST, channel NULLS LAST
        """, [str(REFERENCE / "us-postal-timezones.csv"), UNKNOWN]).fetchall()
    result = {"dateBasis": date_basis, "basis": basis, "dimension": dimension, "national": {},
              "states": {}, "regions": {}, "channels": []}
    place_channels = {}
    for flags, state, region, channel, orders, samples, fallback, average, p90 in rows:
        metrics = {"orders": orders, "samples": samples, "fallback": fallback, "avg": average, "p90": p90}
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
    result["geography"] = states
    return result
