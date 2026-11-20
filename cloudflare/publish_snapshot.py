"""Publish a compact, read-only fulfillment preview to the site's private KV binding."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from starlette.requests import Request as AppRequest

from app import api, core, pages_api, public_preview, tracking_archive
from app.carrier_delivery import DELIVERED_UTC, reporting_facts
from app.order_time import delivery_facts
from app.tracking_clock import carrier_wall_time, utc_key

SLOTS = 1024
GROUP_SIZE = 16
MAX_VERSION_BYTES = 280_000_000
MIN_AGE = timedelta(hours=5, minutes=50)
FIELDS = set(public_preview.DETAIL_FIELDS) | {"最新描述"}
KINDS = ("refs", "orders", "events")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def slot_sql(expression):
    return f"(('0x'||substr(sha256({expression}),1,4))::INTEGER % {SLOTS})"


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str,
                      allow_nan=False).encode()


def rows(result):
    columns = [description[0] for description in result.description]
    while batch := result.fetchmany(6000):
        for row in batch:
            yield dict(zip(columns, row))


class Packages:
    def __init__(self, base):
        self.base = Path(base)
        self.files = {}
        self.total_bytes = 0

    def store(self, value):
        key = sha(value)
        if key not in self.files:
            path = self.base / key
            path.write_bytes(value)
            self.files[key] = path
            self.total_bytes += len(value)
        return key

    def pack(self, result, add):
        output = [None] * (SLOTS // GROUP_SIZE)
        group = -1
        parts = {}

        def write_parts():
            directory = bytearray(GROUP_SIZE * 8)
            blocks = []
            offset = len(directory)
            for position, values in parts.items():
                data = gzip.compress(encode(values), compresslevel=4, mtime=0)
                struct.pack_into(">II", directory, (position % GROUP_SIZE)*8, offset, len(data))
                offset += len(data)
                blocks.append(data)
            output[group] = self.store(b"".join([directory, *blocks]))

        for row in rows(result):
            position = row.pop("position")
            group_id = position // GROUP_SIZE
            if group_id != group:
                if parts:
                    write_parts()
                parts = {}
                group = group_id
            add(parts.setdefault(position, {}), row)
        if parts:
            write_parts()
        return output


def export_refs(con, facts, packages):
    result = con.execute(f"""
        WITH refs AS (
            SELECT 履约单号 AS id, 签出时间 AS dispatched,
                   unnest(list_distinct(['r:' || upper(trim(履约单号)), 'r:' || upper(trim(快递单号)),
                   'r:' || upper(trim(订单号)), 'r:' || upper(trim(平台订单号)),
                   'n:' || 单号归一])) AS ref
            FROM {facts}
        )
        SELECT {slot_sql("ref")} AS position, ref, id FROM refs
        WHERE regexp_full_match(ref, '[rn]:[A-Z0-9#_.+ /-]{{4,128}}')
        ORDER BY position, ref, dispatched DESC NULLS LAST, id
    """)
    return packages.pack(result, lambda group, row: group.setdefault(row["ref"], []).append(row["id"]))


def archive_clocks(con):
    if not core.columns(con, tracking_archive.EVENT_TABLE):
        return "", ("NULL AS last_raw", "NULL AS last_iso", "NULL AS delivered_raw", "NULL AS delivered_iso")
    relation = f"""
        LEFT JOIN (
            SELECT tracking_no, carrier_code,
                   max(try_cast(event_time_utc AS TIMESTAMP)) AS last_utc,
                   arg_max(event_time_raw, event_time_utc) AS last_raw,
                   arg_max(event_time_iso, event_time_utc) AS last_iso,
                   max(try_cast(event_time_utc AS TIMESTAMP)) FILTER (WHERE delivered) AS delivered_utc,
                   arg_max(event_time_raw, event_time_utc) FILTER (WHERE delivered) AS delivered_raw,
                   arg_max(event_time_iso, event_time_utc) FILTER (WHERE delivered) AS delivered_iso
            FROM (SELECT *, stage='Delivered' OR starts_with(substatus,'Delivered_') AS delivered
                  FROM {tracking_archive.EVENT_TABLE})
            GROUP BY tracking_no, carrier_code
        ) a ON a.tracking_no=o.单号归一 AND a.carrier_code=o.回补运输商代码
    """
    columns = tuple(f"CASE WHEN a.{key}_utc={selection} THEN a.{key}_{clock} END AS {key}_{clock}"
                    for key, selection in (("last", "o.最新事件时间"), ("delivered", f"o.{DELIVERED_UTC}"))
                    for clock in ("raw", "iso"))
    return relation, columns


def export_orders(con, facts, packages):
    reporting = reporting_facts(con, delivery_facts(con, facts))
    tracking = public_preview.tracking_projection(con, reporting)
    archive_join, archive_fields = archive_clocks(con)
    detail_fields = FIELDS - {"打包时间", "分配时间", "签收当地时间", "承运商签收时间"}
    columns = [f'o."{name}"' for name in sorted(detail_fields)]
    fields = core.columns(con, "履约单明细")
    steps = [f"max({name}_ts) AS {name}" for name in ("分配时间", "打包时间") if name + "_ts" in fields]
    join_steps = ""
    if steps:
        columns.extend(f"wh.{step.split()[-1]}" for step in steps)
        join_steps = f"LEFT JOIN (SELECT 履约单号, {', '.join(steps)} FROM 履约单明细 GROUP BY 1) wh USING (履约单号)"
    result = con.execute(f"""
        SELECT {slot_sql("o.履约单号")} AS position, {", ".join(columns)},
               o.运输商代码, o.回补运输商代码, o.{DELIVERED_UTC} AS 承运商签收时间,
               CASE WHEN o.tracked_delivery=o.{DELIVERED_UTC} THEN o.tracked_delivery_local END AS 签收当地时间,
               {', '.join(archive_fields)}
        FROM ({tracking}) o {join_steps} {archive_join}
        QUALIFY row_number() OVER (PARTITION BY o.履约单号
          ORDER BY o.最新事件时间 DESC NULLS LAST, o.运输商代码 NULLS LAST)=1
        ORDER BY position, o.履约单号
    """)
    count = 0

    def add(group, row):
        nonlocal count
        count += 1
        platform = row.get("平台")
        if platform in platform_names:
            row["平台名称"] = platform_names[platform]
        row["最新事件时间"] = utc_key(row["最新事件时间"])
        row["承运商签收时间"] = utc_key(row["承运商签收时间"])
        for label, key in (("最新事件当地时间", "last"), ("签收当地时间", "delivered")):
            row[label] = row.get(label) or carrier_wall_time(row.pop(key + "_raw"), row.pop(key + "_iso"))
            row.pop(key + "_raw", None)
            row.pop(key + "_iso", None)
        native = row.pop("运输商代码")
        archive = row.pop("回补运输商代码")
        group[row["履约单号"]] = [{key: value for key, value in row.items() if value is not None}, native, archive]

    platform_names = core.platform_names()
    return packages.pack(result, add), count


def export_events(con, facts, packages):
    sources = []
    fields = core.columns(con, "trk_event")
    if fields:
        carrier = "运输商代码" if "运输商代码" in fields else "NULL"
        local = "当地时间" if "当地时间" in fields else "NULL"
        provider = "事件承运商" if "事件承运商" in fields else "NULL"
        sequence = "-coalesce(原始顺序,0)" if "原始顺序" in fields else "0"
        sources.append(f"""
            SELECT 单号 AS number, 'n' AS kind, {carrier} AS carrier, 事件时间::VARCHAR AS moment,
                   {local} AS wall, NULL AS iso, 主状态 AS stage, 子状态 AS sub,
                   {provider} AS provider, 描述 AS description, 地点 AS location, 0 AS leg, {sequence} AS seq
            FROM trk_event
        """)
    if core.columns(con, tracking_archive.EVENT_TABLE):
        sources.append(f"""
            SELECT tracking_no, 'a', carrier_code, event_time_utc::VARCHAR, event_time_raw, event_time_iso,
                   stage, substatus, provider_name, description, location, provider_position, -event_position
            FROM {tracking_archive.EVENT_TABLE}
        """)
    milestone_clock = "当地时间" if "当地时间" in core.columns(con, "trk_milestone") else "NULL"
    sources.append(f"""
        SELECT 单号, 'm', NULL, 时间::VARCHAR, {milestone_clock}, NULL, 节点, NULL, NULL, NULL, NULL, 0, 0
        FROM trk_milestone WHERE 时间 IS NOT NULL
    """)
    native_numbers = "SELECT DISTINCT 单号 AS number FROM trk_event" if fields else "SELECT NULL::VARCHAR AS number"
    # Specify names even when an older database lacks a source table.
    result = con.execute(f"""
        WITH events(number, kind, carrier, moment, wall, iso, stage, sub, provider, description,
                    location, leg, seq) AS ({" UNION ALL ".join(sources)}), native AS ({native_numbers})
        SELECT {slot_sql("e.number")} AS position, e.* EXCLUDE (leg, seq), n.number IS NOT NULL AS may_overlap
        FROM events e SEMI JOIN {facts} o ON o.单号归一=e.number
        LEFT JOIN native n ON n.number=e.number
        WHERE e.number IS NOT NULL AND e.number <> ''
        ORDER BY position, e.number, kind, carrier, try_cast(moment AS TIMESTAMPTZ) NULLS LAST, leg, seq
    """)

    def add(group, row):
        number, kind, carrier, moment, wall, iso, stage, sub, provider, desc, location, may_overlap = row.values()
        group = group.setdefault(number, {})
        utc = utc_key(moment)
        if kind == "m":
            if stage in public_preview.MILESTONE_NAMES:
                group.setdefault("m", []).append({"节点": stage, "时间": utc, "当地时间": wall})
            return
        if kind == "a":
            wall = carrier_wall_time(wall, iso)
        clean = public_preview.event({"主状态": stage, "子状态": sub})
        # Only the fingerprint is retained for deduplication. Raw notes and locations are omitted.
        marker = None
        if may_overlap:
            marker = sha(encode([utc or wall or "", *[str(value or "").strip()
                                                      for value in (sub, desc, location, provider)]]))[:24]
        carrier_key = "null" if carrier is None else str(carrier)
        group.setdefault(kind, {}).setdefault(carrier_key, []).append(
            [marker, utc, wall, provider, clean["描述"]])

    return packages.pack(result, add)


def create_version(packages):
    start = core.snapshot_key()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    request = AppRequest({"type": "http", "headers": []})
    stats = {
        "meta": api.meta(request),
        "overview": pages_api.overview(request),
        "channels": {basis: pages_api.channels(request, min_n=1, basis=basis)
                     for basis in ("paid", "shipped", "online")},
    }
    with core.duck() as con:
        facts = core.active_facts(core.order_facts(con))
        refs = export_refs(con, facts, packages)
        orders, count = export_orders(con, facts, packages)
        events = export_events(con, facts, packages)
    if core.snapshot_key() != start:
        raise RuntimeError("主库在导出期间已完成更新，请重试快照同步")
    if count != stats["meta"]["range"]["orders"] or count < 1:
        raise RuntimeError("快照的汇总单量与订单索引不一致")
    stat_key = packages.store(gzip.compress(encode(stats), compresslevel=4, mtime=0))
    if packages.total_bytes > MAX_VERSION_BYTES:
        raise RuntimeError(f"快照达到 {packages.total_bytes / 1e6:.1f} MB，超过当前 280 MB 单份预算")
    return {"at": now, "ordersCount": count, "stats": stat_key, "refs": refs, "orders": orders, "events": events}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Publisher:
    def __init__(self, base, edge_key):
        self.base = base.rstrip("/") + "/__edge/snapshot/"
        self.auth = "Bearer " + sha(("fulfillment-snapshot-v1:" + edge_key).encode())

    def send(self, path, method="GET", data=None, headers=None):
        for delay in (0, 2, 5):
            sleep(delay)
            req = Request(self.base + path, method=method, data=data, headers={
                "Authorization": self.auth, "Content-Type": "application/octet-stream",
                "User-Agent": "Fulfillment-Snapshot/1.0", **(headers or {}),
            })
            try:
                with build_opener(NoRedirect()).open(req, timeout=30) as response:
                    return json.load(response), response.headers
            except HTTPError as error:
                if error.code == 503 and path == "manifest":
                    raise RuntimeError("线上尚未绑定 Workers KV，等待发布") from None
                if error.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"快照接口 {method} 返回 HTTP {error.code}") from None
            except (URLError, TimeoutError, ConnectionError):
                continue
        raise RuntimeError(f"快照接口 {method} 暂时不可用，已保留上次版本")

    def cleanup(self):
        cursor = None
        removed = 0
        for _ in range(30):
            query = "?" + urlencode({"cursor": cursor}) if cursor else ""
            result, _ = self.send("cleanup" + query, "POST", b"")
            removed += result["removed"]
            cursor = result["cursor"]
            if not cursor:
                break
        print(f"清理已过期数据块 {removed} 个", flush=True)

    def publish(self, directory, force=False):
        status, headers = self.send("manifest")
        current, previous = status["current"], status["previous"]
        if current and not force and datetime.now(timezone.utc) - datetime.fromisoformat(current["at"]) < MIN_AGE:
            print(f"现有快照 {current['at']}，等待下一次同步窗口", flush=True)
            return
        self.cleanup()
        packages = Packages(directory)
        version = create_version(packages)
        remote = {digest for manifest in (current, previous) if manifest
                  for digest in (manifest["stats"], *[item for kind in KINDS for item in manifest[kind]])
                  if digest}
        needed = {digest: path for digest, path in packages.files.items() if digest not in remote}
        if current and all(current[field] == version[field] for field in ("stats", *KINDS)):
            print("履约单、轨迹和看板未变化，沿用已发布版本", flush=True)
            return
        print(f"导出 {version['ordersCount']:,} 单，{packages.total_bytes/1e6:.1f} MB，上传 {len(needed)} 个变化块", flush=True)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = pool.map(lambda item: self.send("blocks/" + item[0], "PUT", item[1].read_bytes()), needed.items())
            for n, _ in enumerate(results, 1):
                if n % 40 == 0:
                    print(f"已上传 {n}/{len(needed)} 块", flush=True)
        condition = {"If-Match": headers["ETag"]} if current else {"If-None-Match": "*"}
        self.send("manifest", "PUT", encode(version), condition)
        print(f"已发布云端快照 {version['at']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅在本机生成快照并检查大小")
    parser.add_argument("--force", action="store_true", help="立即同步一次")
    args = parser.parse_args()
    os.environ["FULFILLMENT_PUBLIC_PREVIEW"] = "true"
    key = os.getenv("FULFILLMENT_EDGE_KEY", "")
    origin = os.getenv("OPS_SITE_ORIGIN", "")
    with TemporaryDirectory(prefix="fulfillment-preview-") as directory:
        if args.check:
            packages = Packages(directory)
            version = create_version(packages)
            print(f"本机快照 {version['ordersCount']:,} 单，{packages.total_bytes/1e6:.1f} MB，"
                  f"{len(packages.files)} 个数据块，最大块 {max(path.stat().st_size for path in packages.files.values())/1e6:.2f} MB")
        elif key and origin.startswith("https://"):
            Publisher(origin, key).publish(directory, force=args.force)
        else:
            parser.error("线上同步需要配置站点地址和后端凭据")


if __name__ == "__main__":
    main()
