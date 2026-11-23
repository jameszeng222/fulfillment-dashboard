"""Configured group membership for the current fulfillment channel."""
import json
from collections import Counter

from . import core

UNGROUPED = "__ungrouped__"
UNGROUPED_LABEL = "未归组"


def load(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='fulfillment_settings'").fetchone():
        row = db.execute("""SELECT payload FROM fulfillment_settings
                            WHERE kind='channel_group' AND name='overview'""").fetchone()
        if row:
            return json.loads(row[0])
    return {"revision": 0, "groups": [
        {"name": "7日达", "channels": ["WYT-WF7日达_Zonal_TK", "WYT-WF7日达Zonal",
                                      "WYT-WF7日达_Zonal", "WYT-WF7日达_Zonal_WCTK"]},
        {"name": "LUVME EXPRESS 3PE", "channels": ["Luvmexpress_3pe_XC01", "Luvmexpress_3pe_JY01",
                                                  "Luvme Express 3pe网红"]},
    ]}


def filter_values(value, groups):
    """Return channels to include and, for 未归组, the configured channels to exclude."""
    selected = set(core.filter_values(value))
    if not selected:
        return None, None
    included = {key for group in groups if group["name"] in selected for key in group["channels"]}
    excluded = None
    if UNGROUPED in selected:
        excluded = {key for group in groups if group["name"] not in selected for key in group["channels"]}
    return tuple(sorted(included)), None if excluded is None else tuple(sorted(excluded))


def predicate(column, selection, *, sqlite=False):
    included, excluded = selection
    if excluded is None and included is None:
        return "", []
    values = included if excluded is None else excluded
    if not values:
        return ("1=0" if excluded is None else ""), []
    operator = "IN" if excluded is None else "NOT IN"
    names = "SELECT value FROM json_each(?)" if sqlite else "SELECT unnest(CAST(?::JSON AS VARCHAR[]))"
    return f"coalesce(trim({column}),'') {operator} ({names})", [json.dumps(values, ensure_ascii=False)]


def distribution(groups, pairs, total):
    counts = Counter()
    for channel, count in pairs:
        counts[(channel or "").strip()] += count
    result = [{"v": group["name"], "t": group["name"],
               "n": sum(counts[key] for key in group["channels"])} for group in groups]
    result.append({"v": UNGROUPED, "t": UNGROUPED_LABEL, "n": total - sum(item["n"] for item in result)})
    return sorted(result, key=lambda item: (-item["n"], item["t"]))
