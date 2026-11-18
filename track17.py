"""
17track 轨迹拉取（**只拉不注册**）。

前提：单号由 ERP 同步注册到 17track 账号，本脚本只负责把轨迹和状态拉回来。
未注册的单号会被平台拒绝（-18019902），记录下来跳过，不重复注册。

用法：
    $env:TRACK17_TOKEN = "xxx"
    python track17.py plan                  # 看要拉多少、按承运商分布
    python track17.py poll --limit 400      # 拉（每次 40 个一批）
    python track17.py poll --days 7         # 只拉最近 7 天创建的单
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
  ** 没有 events[]，详细事件用 milestone 代替 **
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
API = "https://api.17track.net/track/v2.2"
BATCH = 40
RATE_SLEEP = 0.35

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

STAGES = ["InfoReceived", "PickedUp", "Departure", "Arrival", "AvailableForPickup",
          "OutForDelivery", "Delivered", "Returning", "Returned"]

DDL = """
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

CREATE OR REPLACE VIEW trk_order_link AS
WITH o AS (
    SELECT 履约单号,
           any_value(快递单号) 快递单号,
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
    -- 17track 口径的尾程天数（从揽收算起，比 WMS 自证更可信）
    CASE WHEN s.签收时间 IS NOT NULL AND o.签出时间_ts IS NOT NULL
         THEN round(date_diff('minute', o.签出时间_ts, s.签收时间) / 1440.0, 2) END AS 承运商尾程天,
    CASE
      WHEN s.签收时间 IS NOT NULL AND o.WMS签收时间 IS NOT NULL THEN '双方已签'
      WHEN s.签收时间 IS NOT NULL AND o.WMS签收时间 IS NULL   THEN '承运商已签_WMS未签'
      WHEN s.签收时间 IS NULL     AND o.WMS签收时间 IS NOT NULL THEN 'WMS已签_承运商未见'
      WHEN m.单号 IS NOT NULL                                  THEN '未注册'
      ELSE '双方均未签'
    END AS 签收对账
FROM o
LEFT JOIN trk_status s ON s.单号 = upper(regexp_replace(o.快递单号, '[^A-Za-z0-9]', '', 'g'))
LEFT JOIN trk_missing m ON m.单号 = upper(regexp_replace(o.快递单号, '[^A-Za-z0-9]', '', 'g'))
"""


def norm(no: str) -> str:
    return "".join(ch for ch in str(no or "").upper() if ch.isalnum())


def connect(db: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db)
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
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


# ---------------------------------------------------------------- 候选

def candidates(con, limit=0, days=0, carriers=None):
    """在途单号（未签收、未取消、有单号），按 单号 去重"""
    where = [
        "出库单状态 IN ('已签出','物流异常')",
        "签收时间_ts IS NULL",
        "coalesce(trim(快递单号),'') <> ''",
    ]
    if days:
        where.append(f"创建日期 >= (SELECT max(创建日期) FROM 履约单明细) - INTERVAL {int(days)} DAY")
    if carriers:
        lst = ",".join("'" + c.replace("'", "''") + "'" for c in carriers)
        where.append(f"物流服务商 IN ({lst})")
    sql = f"""
        SELECT 快递单号, any_value(物流服务商) 承运商, max(创建日期) 最新创建
        FROM 履约单明细
        WHERE {' AND '.join(where)}
        GROUP BY 快递单号
        ORDER BY 最新创建 DESC
    """
    rows = con.execute(sql).fetchall()
    out, seen = [], set()
    for tn, car, _ in rows:
        n = norm(tn)
        if not (8 <= len(n) <= 50) or "+" in str(tn) or not n.isascii():
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append({"单号": n, "承运商": car or "(未分配)"})
        if limit and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 命令

def cmd_plan(con, args):
    cands = candidates(con, days=args.days)
    print(f"在途单号: {len(cands):,} 个\n")
    stat = {}
    for c in cands:
        stat[c["承运商"]] = stat.get(c["承运商"], 0) + 1
    print(f"{'承运商':<20}{'单号数':>10}")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"{k:<20}{v:>10,}")
    n_batch = -(-len(cands) // BATCH)
    print(f"\n分批: {n_batch:,} 批 × {BATCH} 个，限速约 {n_batch*RATE_SLEEP/60:.0f} 分钟")
    print("查询不扣配额（配额只用于注册），受 3 req/s 限流。")


def cmd_poll(con, args):
    token = token_of(args)
    cands = candidates(con, limit=args.limit, days=args.days,
                       carriers=args.carrier.split(",") if args.carrier else None)
    if not cands:
        print("没有需要拉取的单号。")
        return
    total_batch = -(-len(cands) // BATCH)
    print(f"开始拉取 {len(cands):,} 个单号（{total_batch} 批）…\n")

    n_ok = n_miss = n_err = 0
    for i in range(0, len(cands), BATCH):
        chunk = cands[i:i + BATCH]
        try:
            resp = post("gettrackinfo", token, [{"number": c["单号"]} for c in chunk])
        except RuntimeError as e:
            print(f"  批次 {i//BATCH+1}: 请求失败 {e}")
            n_err += len(chunk)
            time.sleep(1)
            continue

        errs = (resp.get("data") or {}).get("errors") or []
        if errs:
            print(f"  批次 {i//BATCH+1}: 平台错误 {errs[0]}")

        for a in (resp.get("data") or {}).get("accepted", []) or []:
            save_one(con, a)
            n_ok += 1
        for rj in (resp.get("data") or {}).get("rejected", []) or []:
            code = (rj.get("error") or {}).get("code")
            n = norm(rj.get("number"))
            if code == -18019902:
                n_miss += 1
                con.execute("""
                    INSERT INTO trk_missing (单号, carrier, 首次发现, 末次检查, 检查次数)
                    VALUES (?,?,?,?,1)
                    ON CONFLICT (单号) DO UPDATE SET
                        末次检查 = excluded.末次检查,
                        检查次数 = trk_missing.检查次数 + 1
                """, [n, (rj.get("error") or {}).get("carrier") or 0, datetime.now(), datetime.now()])
            else:
                n_err += 1

        print(f"  批次 {i//BATCH+1}/{total_batch}: 成功 {n_ok} 未注册 {n_miss} 其它错误 {n_err}")
        time.sleep(RATE_SLEEP)

    print(f"\n完成：成功 {n_ok:,}，未注册 {n_miss:,}，其它错误 {n_err:,}")
    _summary(con)


def save_one(con, a: dict):
    n = norm(a.get("number"))
    ti = a.get("track_info") or {}
    st = ti.get("latest_status") or {}
    le = ti.get("latest_event") or {}
    tm = ti.get("time_metrics") or {}
    edd = (tm.get("estimated_delivery_date") or {})
    ms = ti.get("milestone") or []

    delivered_at = None
    for m in ms:
        if m.get("key_stage") == "Delivered" and m.get("time_utc"):
            t = parse_dt(m["time_utc"])
            if t and (delivered_at is None or t > delivered_at):
                delivered_at = t

    status = st.get("status") or ""
    con.execute("""
        INSERT INTO trk_status (
            单号, carrier, 状态, 状态中文, 子状态, 最新事件时间, 最新描述, 地点,
            是否签收, 签收时间, 运输天数, 下单后天数, 停滞天数, EDD起, EDD止, 风险因子, 查询时间)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (单号) DO UPDATE SET
            carrier=excluded.carrier, 状态=excluded.状态, 状态中文=excluded.状态中文,
            子状态=excluded.子状态, 最新事件时间=excluded.最新事件时间,
            最新描述=excluded.最新描述, 地点=excluded.地点, 是否签收=excluded.是否签收,
            签收时间=excluded.签收时间, 运输天数=excluded.运输天数,
            下单后天数=excluded.下单后天数, 停滞天数=excluded.停滞天数,
            EDD起=excluded.EDD起, EDD止=excluded.EDD止,
            风险因子=excluded.风险因子, 查询时间=excluded.查询时间
    """, [n, a.get("carrier"), status, STATUS_CN.get(status, status), st.get("sub_status"),
          parse_dt(le.get("time_utc")), (le.get("description") or "")[:300], (le.get("location") or "")[:120],
          status == "Delivered", delivered_at,
          tm.get("days_of_transit"), tm.get("days_after_order"), tm.get("days_after_last_update"),
          parse_dt(str(edd.get("from"))[:10]) if edd.get("from") else None,
          parse_dt(str(edd.get("to"))[:10]) if edd.get("to") else None,
          (ti.get("misc_info") or {}).get("risk_factor"),
          datetime.now()])

    for m in ms:
        t = parse_dt(m.get("time_utc"))
        if not t:
            continue
        con.execute("""
            INSERT INTO trk_milestone (单号, 节点, 时间) VALUES (?,?,?)
            ON CONFLICT (单号, 节点) DO UPDATE SET 时间 = excluded.时间
        """, [n, m.get("key_stage"), t])

    con.execute("DELETE FROM trk_missing WHERE 单号 = ?", [n])


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
    ap.add_argument("--days", type=int, default=0, help="只处理最近 N 天创建的单")
    ap.add_argument("--carrier", help="只处理指定承运商，逗号分隔")
    args = ap.parse_args()

    con = connect(args.db)
    try:
        return {"plan": cmd_plan, "poll": cmd_poll,
                "status": cmd_status, "selftest": cmd_selftest}[args.cmd](con, args) or 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
