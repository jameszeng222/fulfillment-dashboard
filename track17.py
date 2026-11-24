"""
17track 轨迹拉取（**只拉不注册**）。

前提：单号由 ERP 同步注册到 17track 账号，本脚本只负责把轨迹和状态拉回来。
按 ERP 的 C 端物流商名称匹配 17TRACK 运输商代码，查询时同时传运单号和代码。
对已知可能登记在其他运输商下的单号，查不到后使用对应代码复查，不重复注册。

用法：
    $env:TRACK17_TOKEN = "xxx"
    python track17.py plan                  # 看要拉多少、按承运商分布
    python track17.py poll --limit 400      # 拉（每次 40 个一批）
    python track17.py poll --days 7         # 只拉最近 7 天创建的单
    python track17.py poll --completed --days 7 --limit 40  # 回查近期 ERP 已签收单
    python track17.py status                # 本地统计
    python track17.py selftest              # 用内置样例验证解析（不需要 token）

配额说明：17track 的配额只用于**注册**，gettrackinfo 查询不扣配额，只受 3 req/s 限流。

响应结构（v2.2）：
    track_info.latest_status            {status, sub_status}
    track_info.latest_event             {time_utc, description, location, stage, sub_status}
    track_info.time_metrics             {days_after_order, days_of_transit, days_of_transit_done,
                                         days_after_last_update, estimated_delivery_date{from,to,source}}
    track_info.milestone[]              {key_stage, time_utc}  9 个关键节点
    track_info.misc_info.risk_factor
    track_info.tracking.providers[].events[]  承运商逐条事件，可能为空
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from app.tracking_clock import carrier_wall_time, ensure_display_columns
from app.waybill import key_sql, normalized, shipped_waybill_sql, strip_quantity

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
API = "https://api.17track.net/track/v2.2"
BATCH = 40
RATE_SLEEP = 0.35

# 官方目录：https://res.17track.net/asset/carrier/info/apicarrier.all.json
C_END_CARRIER_CODES = {
    "3pe": 191376,
    "3pe express": 191376,
    "correios brazil": 2151,
    "dhl": 100001,
    "dhl express": 100001,
    "dhl ecommerce us": 7047,
    "fedex": 100003,
    "fedex smartpost": 100003,
    "gofo": 100996,
    "ontrac": 100049,
    "sf express": 100012,
    "speedx": 190844,
    "ups": 100002,
    "ups mail innovations": 100398,
    "ups mail lnnovations": 100398,  # ERP 历史拼写
    "ups mi": 100398,
    "usps": 21051,
    "veho": 101068,
    "xs express": 190477,
    "yanwen": 190012,
    "yunexpress": 190008,
}

# WYT-USPS 的已登记记录也会使用后台承运商或物流产品代码。
WYT_CARRIER_CODES = {"dhl ecommerce": 7047}
WYT_REGISTRATION_FALLBACKS = {"usps": 190284, "ups": 100398, "yanwen": 191278}
REGISTRATION_ALIASES = {
    ("xs_express", "xs express"): 191281,  # XUANSI
    ("hete-logistics", "3pe"): 190008,
    ("hete-logistics", "3pe express"): 190008,  # Existing YunExpress registration
}

STATUS_CN = {
    "NotFound": "查无此单",
    "InfoReceived": "已收到信息",
    "InTransit": "运输中",
    "Expired": "长时间未更新",
    "AvailableForPickup": "待自提",
    "OutForDelivery": "派送中",
    "DeliveryFailure": "投递失败",
    "Delivered": "已签收",
    "Exception": "异常",
}

# 17track 官方状态字典：9 个主状态 + 30 个子状态（中文为官方文档释义）
OFFICIAL_STATUS = [
    # (主状态, 子状态, 中文主状态, 中文子状态)
    ("NotFound", "NotFound_Other", "查无此单", "承运商未返回任何信息"),
    ("NotFound", "NotFound_InvalidCode", "查无此单", "单号无效"),
    ("InfoReceived", "InfoReceived", "已收到信息", "已收到订单信息，等待揽收"),
    ("InTransit", "InTransit_PickedUp", "运输中", "已揽收"),
    ("InTransit", "InTransit_Other", "运输中", "运输中"),
    ("InTransit", "InTransit_Departure", "运输中", "已离开始发地"),
    ("InTransit", "InTransit_Arrival", "运输中", "已到达目的地"),
    ("InTransit", "InTransit_CustomsProcessing", "运输中", "清关中"),
    ("InTransit", "InTransit_CustomsReleased", "运输中", "清关完成"),
    ("InTransit", "InTransit_CustomsRequiringInformation", "运输中", "清关需补充材料"),
    ("Expired", "Expired_Other", "长时间未更新", "超期未更新"),
    ("AvailableForPickup", "AvailableForPickup_Other", "待自提", "已到达取件点"),
    ("OutForDelivery", "OutForDelivery_Other", "派送中", "派送中"),
    ("DeliveryFailure", "DeliveryFailure_Other", "投递失败", "投递失败"),
    ("DeliveryFailure", "DeliveryFailure_NoBody", "投递失败", "投递时收件人不在"),
    ("DeliveryFailure", "DeliveryFailure_Security", "投递失败", "安全/清关/费用问题"),
    ("DeliveryFailure", "DeliveryFailure_Rejected", "投递失败", "收件人拒收"),
    ("DeliveryFailure", "DeliveryFailure_InvalidAddress", "投递失败", "地址错误无法投递"),
    ("Delivered", "Delivered_Other", "已签收", "已妥投签收"),
    ("Exception", "Exception_Other", "异常", "异常"),
    ("Exception", "Exception_Returning", "异常", "退件中"),
    ("Exception", "Exception_Returned", "异常", "已退回寄件人"),
    ("Exception", "Exception_NoBody", "异常", "查无收件人"),
    ("Exception", "Exception_Security", "异常", "安全/清关/费用异常"),
    ("Exception", "Exception_Damage", "异常", "运输中损坏"),
    ("Exception", "Exception_Rejected", "异常", "派送前被拒收"),
    ("Exception", "Exception_Delayed", "异常", "延误"),
    ("Exception", "Exception_Lost", "异常", "丢件"),
    ("Exception", "Exception_Destroyed", "异常", "无法投递已销毁"),
    ("Exception", "Exception_Cancel", "异常", "订单已取消"),
]

STAGES = ["InfoReceived", "PickedUp", "Departure", "Arrival", "AvailableForPickup",
          "OutForDelivery", "Delivered", "Returning", "Returned"]

DDL = f"""
CREATE TABLE IF NOT EXISTS trk_status (
    单号            VARCHAR PRIMARY KEY,
    carrier         INTEGER,
    状态            VARCHAR,
    状态中文        VARCHAR,
    子状态          VARCHAR,
    最新事件时间    TIMESTAMP,
    最新描述        VARCHAR,
    地点            VARCHAR,
    是否签收        BOOLEAN,
    签收时间        TIMESTAMP,
    运输天数        INTEGER,
    下单后天数      INTEGER,
    停滞天数        INTEGER,
    EDD起           DATE,
    EDD止           DATE,
    风险因子        INTEGER,
    查询时间        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dim_trk_status (
    主状态      VARCHAR,
    子状态      VARCHAR,
    主状态中文  VARCHAR,
    子状态中文  VARCHAR,
    是否终结    BOOLEAN,
    是否异常    BOOLEAN,
    PRIMARY KEY (主状态, 子状态)
);

CREATE TABLE IF NOT EXISTS trk_milestone (
    单号  VARCHAR,
    节点  VARCHAR,
    时间  TIMESTAMP,
    PRIMARY KEY (单号, 节点)
);

CREATE TABLE IF NOT EXISTS trk_missing (
    单号        VARCHAR PRIMARY KEY,
    carrier     INTEGER,
    首次发现    TIMESTAMP,
    末次检查    TIMESTAMP,
    检查次数    INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS trk_event (
    单号 VARCHAR, 事件时间 TIMESTAMP, 主状态 VARCHAR, 子状态 VARCHAR,
    描述 VARCHAR, 地点 VARCHAR, 是否签收 BOOLEAN, 入库时间 TIMESTAMP
);

CREATE OR REPLACE VIEW trk_order_link AS
WITH o AS (
    SELECT 履约单号,
           {shipped_waybill_sql()} 快递单号,
           any_value(物流服务商) 物流服务商,
           any_value(仓库代码) 仓库代码,
           any_value(目的国家英文名称) 目的国,
           any_value(出库单状态) 出库单状态,
           any_value(签出时间_ts) 签出时间_ts,
           any_value(签收时间_ts) WMS签收时间,
           any_value(创建日期) 创建日期
    FROM 履约单明细 GROUP BY 1
)
SELECT
    o.履约单号, o.快递单号, o.物流服务商, o.仓库代码, o.目的国,
    o.出库单状态, o.创建日期, o.签出时间_ts, o.WMS签收时间,
    s.状态 AS 轨迹状态, s.状态中文, s.子状态, s.最新事件时间, s.最新描述, s.地点,
    s.签收时间 AS 承运商签收时间, s.运输天数, s.停滞天数, s.风险因子, s.查询时间 AS 轨迹查询时间,
    -- ERP 签出为北京时间，17TRACK 签收为 UTC。
    CASE WHEN s.签收时间 IS NOT NULL AND o.签出时间_ts IS NOT NULL
         THEN round(date_diff('minute', o.签出时间_ts - INTERVAL '8 hours', s.签收时间) / 1440.0, 2) END AS 承运商尾程天,
    CASE
      WHEN s.签收时间 IS NOT NULL AND o.WMS签收时间 IS NOT NULL THEN '双方已签'
      WHEN s.签收时间 IS NOT NULL AND o.WMS签收时间 IS NULL   THEN '承运商已签_WMS未签'
      WHEN s.签收时间 IS NULL     AND o.WMS签收时间 IS NOT NULL THEN 'WMS已签_承运商未见'
      WHEN m.单号 IS NOT NULL                                  THEN '未注册'
      ELSE '双方均未签'
    END AS 签收对账
FROM o
LEFT JOIN trk_status s ON s.单号 = {key_sql("o.快递单号")}
LEFT JOIN trk_missing m ON m.单号 = {key_sql("o.快递单号")}
"""


def norm(no: str) -> str:
    return normalized(no)


def connect(db: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db)
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    for column, kind in (("运输商代码", "INTEGER"), ("事件承运商代码", "INTEGER"),
                         ("事件承运商", "VARCHAR"), ("事件键", "VARCHAR"), ("原始顺序", "INTEGER")):
        con.execute(f"ALTER TABLE trk_event ADD COLUMN IF NOT EXISTS {column} {kind}")
    ensure_display_columns(con)
    con.execute("CREATE INDEX IF NOT EXISTS ix_trk_event_number ON trk_event(单号)")
    # 官方 9 主 + 30 子状态字典，每次连接时刷新（幂等）
    con.executemany(
        "INSERT OR REPLACE INTO dim_trk_status VALUES (?,?,?,?,?,?)",
        [(m, sub, cn_m, cn_s,
          m in ("Delivered", "Exception", "Expired"),
          m in ("Exception", "DeliveryFailure", "Expired", "NotFound"))
         for m, sub, cn_m, cn_s in OFFICIAL_STATUS])
    return con


def token_of(args) -> str:
    t = (getattr(args, "token", None) or os.environ.get("TRACK17_TOKEN") or "").strip()
    if not t:
        raise SystemExit('缺少 token。设置环境变量后重试：$env:TRACK17_TOKEN = "你的key"')
    return t


def post(path: str, token: str, payload) -> dict:
    req = urllib.request.Request(
        f"{API}/{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"17token": token, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"读取 17TRACK 响应失败：{e}") from e


def parse_dt(s):
    """接受 '2026-11-17T21:18:11Z' / '2026-11-17 21:18:11' / '2026-11-17' 三种形态"""
    if not s:
        return None
    raw = str(s).replace("T", " ").replace("Z", "").strip()
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    return None


def tracking_utc(event):
    """Use the documented UTC field, or ISO time only when it has an offset."""
    for field in ("time_utc", "time_iso"):
        raw = event.get(field)
        if not isinstance(raw, str) or len(raw.strip()) < 16:
            continue
        try:
            timestamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(timezone.utc)
        if field == "time_utc":
            return timestamp.replace(tzinfo=timezone.utc)
    return None


def _stored_utc(event):
    timestamp = tracking_utc(event)
    return timestamp.replace(tzinfo=None) if timestamp is not None else None


def local_event_time(event):
    return carrier_wall_time(event.get("time_raw"), event.get("time_iso"))


# ---------------------------------------------------------------- 候选

def carrier_lookup(channel, c_end, previous_code, has_event):
    channel = (channel or "").strip().casefold()
    if channel == "postnet":
        return None, None

    name = (c_end or "").strip().casefold()
    mapped_code = C_END_CARRIER_CODES.get(name)
    fallback = REGISTRATION_ALIASES.get((channel, name))
    if channel == "wyt-usps":
        mapped_code = mapped_code or WYT_CARRIER_CODES.get(name)
        fallback = WYT_REGISTRATION_FALLBACKS.get(name)

    # 有轨迹的历史记录已经验证过当时的登记渠道，优先从该渠道继续同步。
    primary = previous_code if previous_code and has_event else mapped_code or previous_code
    alternate = next(
        (code for code in (mapped_code, fallback, previous_code) if code and code != primary),
        None,
    )
    return primary, alternate


def candidates(con, limit=0, days=0, carriers=None, completed=False):
    """按单号去重，默认查在途。completed 查 ERP 已签收的履约。"""
    has_facts = con.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema=current_schema() AND table_name='fct_order'
    """).fetchone()[0]
    facts = """SELECT 快递单号, 物流商 AS 承运商, C端承运商, 创建日期, 签出时间, 签收时间, 状态
               FROM fct_order""" if has_facts else f"""
        SELECT {shipped_waybill_sql()} AS 快递单号,
               any_value(物流服务商) AS 承运商,
               any_value(nullif(trim("C端显示物流服务商名称"),'')) AS C端承运商,
               min(创建日期) AS 创建日期, min(签出时间_ts) AS 签出时间,
               max(签收时间_ts) AS 签收时间, any_value(出库单状态) AS 状态
        FROM 履约单明细 GROUP BY 履约单号
    """
    where = [
        "coalesce(trim(c.快递单号),'') <> ''",
        "lower(coalesce(trim(c.承运商),'')) <> 'postnet'",
        "lower(coalesce(trim(c.C端承运商),'')) <> 'postnet'",
        ("c.签收时间 IS NOT NULL AND coalesce(c.状态,'') <> '已取消'" if completed else
         """c.签收时间 IS NULL AND coalesce(c.状态,'') NOT IN ('已取消','已完成')
            AND (c.签出时间 IS NOT NULL OR c.状态 IN ('已签出','物流异常'))"""),
    ]
    args = []
    activity = "c.签收时间::DATE" if completed else "coalesce(c.签出时间::DATE,c.创建日期)"
    if days:
        where.append(f"{activity} >= (SELECT max({activity}) FROM orders c) - INTERVAL {int(days)} DAY")
    if carriers:
        where.append(f"c.承运商 IN ({','.join('?' for _ in carriers)})")
        args.extend(carriers)
    sql = f"""
        SELECT c.快递单号, c.承运商, c.C端承运商, nullif(t.carrier, 0),
               t.最新事件时间 IS NOT NULL
        FROM orders c
        LEFT JOIN trk_status t
          ON t.单号 = {key_sql("c.快递单号")}
        WHERE {' AND '.join(where)}
        ORDER BY {activity} DESC, c.创建日期 DESC, c.快递单号
    """
    rows = con.execute(f"WITH orders AS ({facts}) {sql}", args).fetchall()
    out, seen = [], set()
    for tn, car, c_end, previous_code, has_event in rows:
        n = norm(tn)
        if not (8 <= len(n) <= 50) or "+" in strip_quantity(tn):
            continue
        if n in seen:
            continue
        seen.add(n)
        code, alternate = carrier_lookup(car, c_end, previous_code, has_event)
        out.append({"单号": n, "承运商": car or "(未分配)", "C端承运商": c_end,
                    "运输商代码": code, "备选运输商代码": alternate})
        if limit and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 命令

def cmd_plan(con, args):
    cands = candidates(con, limit=args.limit, days=args.days,
                       carriers=args.carrier.split(",") if args.carrier else None,
                       completed=args.completed)
    scope = "已签收" if args.completed else "在途"
    print(f"{scope}单号: {len(cands):,} 个\n")
    stat = {}
    for c in cands:
        stat[c["承运商"]] = stat.get(c["承运商"], 0) + 1
    print(f"{'承运商':<20}{'单号数':>10}")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"{k:<20}{v:>10,}")
    with_code = sum(c["运输商代码"] is not None for c in cands)
    print(f"\n带运输商代码查询 {with_code:,} 个，仅按单号查询 {len(cands) - with_code:,} 个。")
    n_batch = -(-len(cands) // BATCH)
    print(f"\n分批: {n_batch:,} 批 × {BATCH} 个，限速约 {n_batch*RATE_SLEEP/60:.0f} 分钟")
    print("查询不扣配额（配额只用于注册），受 3 req/s 限流。")


def get_trackinfo(token, codes):
    payload = [{"number": number, "carrier": code} if code else {"number": number}
               for number, code in codes.items()]
    response = post("gettrackinfo", token, payload)
    data = response.get("data") or {}
    errors = data.get("errors") or []
    if response.get("code", 0) != 0 or errors:
        raise RuntimeError(f"平台错误 {errors[0] if errors else response.get('code')}")

    accepted = {}
    for item in data.get("accepted") or []:
        number = norm(item.get("number"))
        if number in codes and (not codes[number] or str(item.get("carrier")) == str(codes[number])):
            accepted[number] = item

    rejected = {}
    for item in data.get("rejected") or []:
        number = norm(item.get("number"))
        if number in codes and number not in accepted:
            rejected[number] = item
    return accepted, rejected


def cmd_poll(con, args):
    token = token_of(args)
    cands = candidates(con, limit=args.limit, days=args.days,
                       carriers=args.carrier.split(",") if args.carrier else None,
                       completed=args.completed)
    if not cands:
        print("没有需要拉取的单号。")
        return
    total_batch = -(-len(cands) // BATCH)
    print(f"开始拉取 {len(cands):,} 个单号（{total_batch} 批）…\n")

    n_ok = n_miss = n_err = 0
    for i in range(0, len(cands), BATCH):
        chunk = cands[i:i + BATCH]
        codes = {candidate["单号"]: candidate["运输商代码"] for candidate in chunk}
        try:
            accepted, rejected = get_trackinfo(token, codes)
        except RuntimeError as e:
            print(f"  批次 {i//BATCH+1}: 请求失败 {e}")
            n_err += len(chunk)
            time.sleep(1)
            continue

        retry = {}
        for candidate in chunk:
            number = candidate["单号"]
            error = (rejected.get(number) or {}).get("error") or {}
            if error.get("code") == -18019902 and candidate["备选运输商代码"]:
                retry[number] = candidate["备选运输商代码"]
                del rejected[number]
        if retry:
            time.sleep(RATE_SLEEP)
            try:
                retried, retry_rejected = get_trackinfo(token, retry)
                accepted.update(retried)
                rejected.update(retry_rejected)
                codes.update(retry)
            except RuntimeError as e:
                print(f"  批次 {i//BATCH+1}: 备选运输商查询失败 {e}")

        for result in accepted.values():
            save_one(con, result)
        missed = 0
        for number, result in rejected.items():
            if (result.get("error") or {}).get("code") == -18019902:
                missed += 1
                con.execute("""
                    INSERT INTO trk_missing (单号, carrier, 首次发现, 末次检查, 检查次数)
                    VALUES (?,?,?,?,1)
                    ON CONFLICT (单号) DO UPDATE SET
                        carrier = excluded.carrier,
                        末次检查 = excluded.末次检查,
                        检查次数 = trk_missing.检查次数 + 1
                """, [number, codes[number] or result.get("carrier") or 0, datetime.now(), datetime.now()])

        n_ok += len(accepted)
        n_miss += missed
        n_err += len(chunk) - len(accepted) - missed

        print(f"  批次 {i//BATCH+1}/{total_batch}: 接口接受 {n_ok} 登记未匹配 {n_miss} 其它错误 {n_err}")
        time.sleep(RATE_SLEEP)

    print(f"\n完成：接口接受 {n_ok:,}，登记未匹配 {n_miss:,}，其它错误 {n_err:,}")
    _summary(con)


def save_one(con, a: dict):
    n = norm(a.get("number"))
    ti = a.get("track_info") or {}
    st = ti.get("latest_status") or {}
    le = ti.get("latest_event") or {}
    tm = ti.get("time_metrics") or {}
    edd = (tm.get("estimated_delivery_date") or {})
    ms = ti.get("milestone") or []

    deliveries = [event for event in ms if event.get("key_stage") == "Delivered"]
    providers = (ti.get("tracking") or {}).get("providers") or []
    for event in [le, *(event for provider in providers for event in provider.get("events") or [])]:
        if event.get("stage") == "Delivered" or (event.get("sub_status") or "").startswith("Delivered_"):
            deliveries.append(event)
    delivered_at = delivered_local = None
    for event in deliveries:
        instant = _stored_utc(event)
        if instant is not None and (delivered_at is None or instant >= delivered_at):
            local = local_event_time(event)
            if instant != delivered_at or local:
                delivered_at, delivered_local = instant, local

    status = st.get("status") or ""
    con.execute("""
        INSERT INTO trk_status (
            单号, carrier, 状态, 状态中文, 子状态, 最新事件时间, 最新描述, 地点,
            是否签收, 签收时间, 运输天数, 下单后天数, 停滞天数, EDD起, EDD止, 风险因子, 查询时间,
            最新事件当地时间, 签收当地时间)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (单号) DO UPDATE SET
            carrier=excluded.carrier, 状态=excluded.状态, 状态中文=excluded.状态中文,
            子状态=excluded.子状态, 最新事件时间=excluded.最新事件时间,
            最新描述=excluded.最新描述, 地点=excluded.地点, 是否签收=excluded.是否签收,
            签收时间=excluded.签收时间, 运输天数=excluded.运输天数,
            下单后天数=excluded.下单后天数, 停滞天数=excluded.停滞天数,
            EDD起=excluded.EDD起, EDD止=excluded.EDD止,
            风险因子=excluded.风险因子, 查询时间=excluded.查询时间,
            最新事件当地时间=coalesce(excluded.最新事件当地时间,
                CASE WHEN trk_status.最新事件时间=excluded.最新事件时间 THEN trk_status.最新事件当地时间 END),
            签收当地时间=coalesce(excluded.签收当地时间,
                CASE WHEN trk_status.签收时间=excluded.签收时间 THEN trk_status.签收当地时间 END)
    """, [n, a.get("carrier"), status, STATUS_CN.get(status, status), st.get("sub_status"),
          _stored_utc(le), (le.get("description") or "")[:300], (le.get("location") or "")[:120],
          status == "Delivered", delivered_at,
          tm.get("days_of_transit"), tm.get("days_after_order"), tm.get("days_after_last_update"),
          parse_dt(str(edd.get("from"))[:10]) if edd.get("from") else None,
          parse_dt(str(edd.get("to"))[:10]) if edd.get("to") else None,
          (ti.get("misc_info") or {}).get("risk_factor"),
          datetime.now(), local_event_time(le), delivered_local])

    for m in ms:
        t = _stored_utc(m)
        if not t:
            continue
        con.execute("""
            INSERT INTO trk_milestone (单号, 节点, 时间, 当地时间) VALUES (?,?,?,?)
            ON CONFLICT (单号, 节点) DO UPDATE SET 时间 = excluded.时间,
                当地时间=coalesce(excluded.当地时间,
                    CASE WHEN trk_milestone.时间=excluded.时间 THEN trk_milestone.当地时间 END)
        """, [n, m.get("key_stage"), t, local_event_time(m)])

    save_events(con, n, a.get("carrier"), ti)
    con.execute("DELETE FROM trk_missing WHERE 单号 = ?", [n])


def save_events(con, number, lookup_code, track_info):
    """保存每条承运商事件，多次拉取时保留已经取得的历史记录。"""
    providers = (track_info.get("tracking") or {}).get("providers") or []
    if not any(group.get("events") for group in providers):
        return
    lookup_code = int(lookup_code or 0)
    existing = {row[0] for row in con.execute(
        "SELECT 事件键 FROM trk_event WHERE 单号=? AND 运输商代码=?", [number, lookup_code],
    ).fetchall()}
    counts = Counter()
    rows = []
    display_updates = []
    received = datetime.now()
    for group in providers:
        provider = group.get("provider") or {}
        provider_code = provider.get("key")
        name = provider.get("name") or provider.get("alias")
        for sequence, event in enumerate(group.get("events") or []):
            event_time = event.get("time_utc") or event.get("time_iso")
            stage = event.get("stage")
            sub = event.get("sub_status")
            desc = event.get("description")
            location = event.get("location")
            fingerprint = json.dumps([provider_code, name, event_time or event.get("time_raw"),
                                      stage, sub, desc, location], ensure_ascii=False, sort_keys=True)
            counts[fingerprint] += 1
            key = hashlib.sha256(f"{fingerprint}:{counts[fingerprint]}".encode()).hexdigest()
            local = local_event_time(event)
            if key in existing:
                if local:
                    display_updates.append((local, number, lookup_code, key))
                continue
            timestamp = _stored_utc(event)
            rows.append((number, timestamp, stage, sub, desc, location, stage == "Delivered", received,
                         lookup_code, provider_code, name, key, sequence, local))
    if rows:
        con.executemany("""
            INSERT INTO trk_event (单号,事件时间,主状态,子状态,描述,地点,是否签收,入库时间,
                                   运输商代码,事件承运商代码,事件承运商,事件键,原始顺序,当地时间)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
    if display_updates:
        con.executemany("""
            UPDATE trk_event SET 当地时间=?
            WHERE 单号=? AND 运输商代码=? AND 事件键=? AND 当地时间 IS NULL
        """, display_updates)


def cmd_status(con, args):
    n = con.execute("SELECT count(*) FROM trk_status").fetchone()[0]
    miss = con.execute("SELECT count(*) FROM trk_missing").fetchone()[0]
    ms = con.execute("SELECT count(*) FROM trk_milestone").fetchone()[0]
    print(f"轨迹状态: {n:,} 个单号")
    print(f"未注册  : {miss:,} 个单号")
    print(f"节点记录: {ms:,} 条")
    if n:
        print("\n按状态：")
        for s, c in con.execute("""
            SELECT coalesce(状态中文, 状态, '(空)'), count(*) FROM trk_status
            GROUP BY 1 ORDER BY 2 DESC""").fetchall():
            print(f"  {s:<14}{c:>8,}")
        r = con.execute("""
            SELECT round(avg(运输天数), 2), round(quantile_cont(运输天数, 0.9), 2), count(*)
            FROM trk_status WHERE 运输天数 IS NOT NULL""").fetchone()
        print(f"\n17track 口径运输天数: 均值 {r[0]} 天 / P90 {r[1]} 天（样本 {r[2]:,}）")
        print("\n签收对账：")
        for s, c in con.execute("""
            SELECT 签收对账, count(*) FROM trk_order_link GROUP BY 1 ORDER BY 2 DESC""").fetchall():
            print(f"  {s:<22}{c:>8,}")


# ---------------------------------------------------------------- 自测

SAMPLE = {
    "number": "TEST123456789", "carrier": 100049,
    "track_info": {
        "latest_status": {"status": "Delivered", "sub_status": "Delivered_Other"},
        "latest_event": {"time_utc": "2026-11-17T21:18:11Z", "description": "Package Delivered",
                         "location": "PORTAGE, MI, US"},
        "time_metrics": {"days_of_transit": 2, "days_after_order": 3, "days_after_last_update": 0,
                         "estimated_delivery_date": {"from": "2026-11-17", "to": "2026-11-18", "source": "ai"}},
        "milestone": [
            {"key_stage": "InfoReceived", "time_utc": "2026-11-15T19:49:10Z"},
            {"key_stage": "PickedUp", "time_utc": "2026-11-16T11:39:23Z"},
            {"key_stage": "Delivered", "time_utc": "2026-11-17T21:18:11Z"},
        ],
        "misc_info": {"risk_factor": 0},
    },
}


def cmd_selftest(con, args):
    ok = True

    def check(c, label):
        nonlocal ok
        ok = ok and c
        print(f"  {'✅' if c else '❌'} {label}")

    save_one(con, SAMPLE)
    r = con.execute("""SELECT 状态, 状态中文, 是否签收, 签收时间, 运输天数, EDD止
                       FROM trk_status WHERE 单号='TEST123456789'""").fetchone()
    check(r is not None, "写入 trk_status")
    check(r[0] == "Delivered" and r[1] == "已签收", f"状态解析/映射 -> {r[0]}/{r[1]}")
    check(r[2] is True, "是否签收")
    check(str(r[3])[:10] == "2026-11-17", f"签收时间取自 milestone -> {r[3]}")
    check(r[4] == 2, f"运输天数 -> {r[4]}")
    check(str(r[5])[:10] == "2026-11-18", f"EDD 解析 -> {r[5]}")

    n_ms = con.execute("SELECT count(*) FROM trk_milestone WHERE 单号='TEST123456789'").fetchone()[0]
    check(n_ms == 3, f"milestone 写入 {n_ms} 条")

    # 幂等：再跑一次不重复
    save_one(con, SAMPLE)
    n_ms2 = con.execute("SELECT count(*) FROM trk_milestone WHERE 单号='TEST123456789'").fetchone()[0]
    check(n_ms2 == 3, "重复写入幂等")
    n1 = con.execute("SELECT count(*) FROM trk_status WHERE 单号='TEST123456789'").fetchone()[0]
    check(n1 == 1, "trk_status 不产生重复行")

    con.execute("DELETE FROM trk_status WHERE 单号='TEST123456789'")
    con.execute("DELETE FROM trk_milestone WHERE 单号='TEST123456789'")

    print("\n" + ("自测通过 ✅" if ok else "自测失败 ❌"))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def _summary(con):
    n = con.execute("SELECT count(*) FROM trk_status").fetchone()[0]
    miss = con.execute("SELECT count(*) FROM trk_missing").fetchone()[0]
    print(f"\n库内轨迹: {n:,} 个单号；未注册: {miss:,} 个")


def main() -> int:
    ap = argparse.ArgumentParser(description="17track 轨迹拉取（只拉不注册）")
    ap.add_argument("cmd", choices=["plan", "poll", "status", "selftest"])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--token", help="17track key，默认读环境变量 TRACK17_TOKEN")
    ap.add_argument("--limit", type=int, default=0, help="本次最多拉多少个单号")
    ap.add_argument("--days", type=int, default=0, help="只处理最近 N 天签出或签收的单")
    ap.add_argument("--carrier", help="只处理指定承运商，逗号分隔")
    ap.add_argument("--completed", action="store_true", help="改查 ERP 已签收的历史履约单")
    args = ap.parse_args()

    con = connect(args.db)
    try:
        result = {"plan": cmd_plan, "poll": cmd_poll,
                  "status": cmd_status, "selftest": cmd_selftest}[args.cmd](con, args)
        if args.cmd == "poll":
            from app import analytics_snapshot, core, track_lookup
            if core.columns(con, "fct_order"):
                track_lookup.build(con)
                analytics_snapshot.build(con)
        return result or 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
