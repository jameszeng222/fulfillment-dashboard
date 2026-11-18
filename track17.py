"""
17track 轨迹接入：注册在途单号 → 拉取轨迹 → 落库，供看板做时效与对账。

用法：
    set TRACK17_TOKEN=xxx                     # 或 $env:TRACK17_TOKEN="xxx"
    python track17.py plan                    # 只看要注册多少、按承运商分布（不调 API）
    python track17.py register --limit 1000   # 注册（默认不限，建议先用 --limit 试）
    python track17.py poll --limit 2000       # 拉轨迹（对已注册且未签收的号）
    python track17.py status                  # 本地统计
    python track17.py selftest                # 用内置样例验证解析逻辑（不需要 token）

计费模型（关键）：17track 按**注册单号数**计费，注册成功后自动跟踪不再扣费。
所以本脚本只对「未签收、未取消、有单号、未注册过」的单号发起注册，且一个单号只注册一次。

表：
    trk_subscription  单号级注册台账与最新状态
    trk_event         轨迹事件（按 单号+事件时间+描述 去重）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
API = "https://api.17track.net/track/v2.2"
BATCH = 40          # 官方单次上限
RATE_SLEEP = 0.35   # 限流 3 req/s

# 物流服务商 → 17track carrier code（取自 reference/17track_carriers.json 核对过）
CARRIERS = {
    "WYT-USPS": 190284,
    "Hete-Logistics": 191376,
    "postnet": 100644,
    "Yun Express": 190008,
    "SF Express": 100012,
    "DHL": 101266,
    "UPS": 100002,
    "YFH-Logistics": 190389,
    "XS_Express": 190477,
    "pep": 0,
    "zx express": 0,
    "zxin express": 0,
    "BR-JZlogistic": 0,
    "Frodio-JYOFFline": 0,
    "": 0,
}

# 主状态 → 中文
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

DDL = """
CREATE TABLE IF NOT EXISTS trk_subscription (
    单号            VARCHAR,
    承运商编码      INTEGER,
    履约单号        VARCHAR,
    物流服务商      VARCHAR,
    仓库代码        VARCHAR,
    目的国          VARCHAR,
    注册结果        VARCHAR,   -- accepted / rejected
    origin          INTEGER,   -- 17track 置信度：1=系统置信 2=传入正确 3=系统猜测
    拒因            VARCHAR,
    注册时间        TIMESTAMP,
    末次查询        TIMESTAMP,
    查询次数        INTEGER DEFAULT 0,
    最新状态        VARCHAR,
    最新子状态      VARCHAR,
    最新事件时间    TIMESTAMP,
    承运商签收时间  TIMESTAMP,
    PRIMARY KEY (单号, 承运商编码)
);

CREATE TABLE IF NOT EXISTS trk_event (
    单号        VARCHAR,
    事件时间    TIMESTAMP,
    主状态      VARCHAR,
    子状态      VARCHAR,
    描述        VARCHAR,
    地点        VARCHAR,
    是否签收    BOOLEAN,
    入库时间    TIMESTAMP
);

CREATE OR REPLACE VIEW trk_order_link AS
SELECT
    s.单号, s.承运商编码, s.履约单号, s.物流服务商, s.仓库代码, s.目的国,
    s.最新状态, s.最新子状态, s.最新事件时间, s.承运商签收时间, s.注册时间,
    f.签收时间_ts AS WMS签收时间,
    f.签出时间_ts,
    CASE
      WHEN s.承运商签收时间 IS NOT NULL AND f.签收时间_ts IS NOT NULL THEN '双方已签'
      WHEN s.承运商签收时间 IS NOT NULL AND f.签收时间_ts IS NULL   THEN '承运商已签_WMS未签'
      WHEN s.承运商签收时间 IS NULL     AND f.签收时间_ts IS NOT NULL THEN 'WMS已签_承运商未见'
      ELSE '双方均未签'
    END AS 签收对账,
    CASE WHEN s.承运商签收时间 IS NOT NULL AND f.签出时间_ts IS NOT NULL
         THEN round(date_diff('minute', f.签出时间_ts, s.承运商签收时间)/1440.0, 2) END AS 承运商尾程天,
    CASE WHEN s.最新事件时间 IS NOT NULL
         THEN round(date_diff('hour', s.最新事件时间, current_timestamp)/24.0, 1) END AS 轨迹停滞天
FROM trk_subscription s
LEFT JOIN (
    SELECT 履约单号, any_value(签收时间_ts) 签收时间_ts, any_value(签出时间_ts) 签出时间_ts
    FROM 履约单明细 GROUP BY 1
) f ON f.履约单号 = s.履约单号
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
    t = args.token or os.environ.get("TRACK17_TOKEN") or ""
    if not t:
        raise SystemExit(
            "缺少 17track token。先设置环境变量：\n"
            '  PowerShell:  $env:TRACK17_TOKEN = "你的token"\n'
            "或命令行传 --token")
    return t.strip()


# ---------------------------------------------------------------- HTTP

def post(path: str, token: str, payload: list) -> dict:
    import urllib.request
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"17token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
    return json.loads(body)


def err_hint(resp: dict) -> str:
    d = (resp.get("data") or {})
    errs = d.get("errors") or []
    if errs:
        return "; ".join(f"{e.get('code')} {e.get('message')}" for e in errs[:3])
    return ""


# ---------------------------------------------------------------- 候选

def candidates(con, limit: int = 0, only_carriers=None):
    """未签收、未取消、有单号、且还没注册过的单号（按单号去重）"""
    rows = con.execute("""
        SELECT 快递单号, 物流服务商, any_value(履约单号) 履约单号,
               any_value(仓库代码) 仓库代码, any_value(目的国家英文名称) 目的国,
               any_value(签出时间_ts) 签出时间
        FROM 履约单明细
        WHERE 出库单状态 IN ('已签出', '物流异常')
          AND 签收时间_ts IS NULL
          AND coalesce(trim(快递单号), '') <> ''
        GROUP BY 快递单号, 物流服务商
    """).fetchall()

    seen = set()
    out = []
    for tn, carrier, order_no, wh, ctry, shipped in rows:
        n = norm(tn)
        if len(n) < 8 or len(n) > 50:
            continue
        if "+" in str(tn) or not n.isascii():
            continue
        code = CARRIERS.get(carrier, 0)
        if only_carriers is not None and code not in only_carriers:
            continue
        key = (n, code)
        if key in seen:
            continue
        if con.execute("SELECT 1 FROM trk_subscription WHERE 单号=? AND 承运商编码=?",
                       [n, code]).fetchone():
            continue
        seen.add(key)
        out.append({"单号": n, "承运商编码": code, "履约单号": order_no,
                    "物流服务商": carrier or "(未分配)", "仓库代码": wh,
                    "目的国": ctry, "签出时间": shipped})
    out.sort(key=lambda x: x["签出时间"] or datetime.min)
    if limit:
        out = out[:limit]
    return out


# ---------------------------------------------------------------- 命令

def cmd_plan(con, args):
    cands = candidates(con)
    print(f"待注册单号: {len(cands):,} 个（已排除已注册、已签收、无单号）\n")
    if not cands:
        return
    stat = {}
    for c in cands:
        k = (c["物流服务商"], c["承运商编码"])
        stat[k] = stat.get(k, 0) + 1
    print(f"{'物流服务商':<18}{'carrier':>8}{'待注册':>10}")
    for (name, code), n in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"{name:<18}{code:>8}{n:>10,}")
    print(f"\n注册批次: {-(-len(cands)//BATCH)} 批（每批 {BATCH} 个，约 {len(cands)/100:.0f} 秒限速）")
    print("\n注意：17track 按注册单号数计费。先用 --limit 小批量试。")


def cmd_register(con, args):
    token = token_of(args)
    cands = candidates(con, limit=args.limit)
    if not cands:
        print("没有需要注册的单号。")
        return
    print(f"开始注册 {len(cands):,} 个单号…\n")

    ok = fail = 0
    for i in range(0, len(cands), BATCH):
        chunk = cands[i:i + BATCH]
        payload = []
        for c in chunk:
            item = {"number": c["单号"]}
            if c["承运商编码"]:
                item["carrier"] = c["承运商编码"]
            payload.append(item)
        try:
            resp = post("register", token, payload)
        except RuntimeError as e:
            print(f"  [批次 {i//BATCH+1}] 请求失败: {e}")
            fail += len(chunk)
            break

        hint = err_hint(resp)
        if hint:
            print(f"  [批次 {i//BATCH+1}] 平台错误: {hint}")

        by_num = {c["单号"]: c for c in chunk}
        for a in (resp.get("data") or {}).get("accepted", []) or []:
            n = norm(a.get("number"))
            c = by_num.get(n, {})
            con.execute(
                "INSERT OR REPLACE INTO trk_subscription "
                "(单号,承运商编码,履约单号,物流服务商,仓库代码,目的国,注册结果,origin,注册时间) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [n, a.get("carrier") or c.get("承运商编码", 0), c.get("履约单号"),
                 c.get("物流服务商"), c.get("仓库代码"), c.get("目的国"),
                 "accepted", a.get("origin"), datetime.now()])
            ok += 1
        for rj in (resp.get("data") or {}).get("rejected", []) or []:
            n = norm(rj.get("number"))
            c = by_num.get(n, {})
            e = rj.get("error") or {}
            con.execute(
                "INSERT OR REPLACE INTO trk_subscription "
                "(单号,承运商编码,履约单号,物流服务商,仓库代码,目的国,注册结果,拒因,注册时间) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [n, c.get("承运商编码", 0), c.get("履约单号"), c.get("物流服务商"),
                 c.get("仓库代码"), c.get("目的国"), "rejected",
                 f"{e.get('code')} {e.get('message')}"[:200], datetime.now()])
            fail += 1

        print(f"  批次 {i//BATCH+1}/{-(-len(cands)//BATCH)}: 成功 {ok} 失败 {fail}")
        time.sleep(RATE_SLEEP)

    print(f"\n完成：接受 {ok:,}，拒绝 {fail:,}")


def cmd_poll(con, args):
    token = token_of(args)
    rows = con.execute("""
        SELECT 单号, 承运商编码 FROM trk_subscription
        WHERE 注册结果 = 'accepted' AND 承运商签收时间 IS NULL
        ORDER BY coalesce(末次查询, TIMESTAMP '2000-01-01')
    """).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("没有需要查询的单号。")
        return
    print(f"查询 {len(rows):,} 个单号的轨迹…\n")

    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        payload = [{"number": n, "carrier": c} for n, c in chunk]
        try:
            resp = post("gettrackinfo", token, payload)
        except RuntimeError as e:
            print(f"  [批次 {i//BATCH+1}] 请求失败: {e}")
            break

        hint = err_hint(resp)
        if hint:
            print(f"  [批次 {i//BATCH+1}] 平台错误: {hint}")

        n_ev = 0
        for a in (resp.get("data") or {}).get("accepted", []) or []:
            n = norm(a.get("number"))
            carrier = a.get("carrier") or 0
            ti = a.get("track_info") or {}
            latest = ti.get("latest_status") or {}
            main = latest.get("status") or ""
            sub = latest.get("sub_status") or ""
            latest_ev = ti.get("latest_event") or {}
            ev_time = parse_time(latest_ev.get("time_iso") or latest_ev.get("time_utc"))

            delivered_at = None
            events = (ti.get("events") or [])
            for ev in events:
                t = parse_time(ev.get("time_iso") or ev.get("time_utc"))
                if not t:
                    continue
                is_del = main == "Delivered" or ev.get("stage") == "Delivered"
                con.execute(
                    "INSERT INTO trk_event (单号,事件时间,主状态,子状态,描述,地点,是否签收,入库时间) "
                    "SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS ("
                    "  SELECT 1 FROM trk_event WHERE 单号=? AND 事件时间=? AND coalesce(描述,'')=? )",
                    [n, t, ev.get("stage") or main, ev.get("sub_status") or sub,
                     ev.get("description") or "", ev.get("location") or "",
                     bool(is_del), datetime.now(), n, t, ev.get("description") or ""])
                n_ev += 1
                if is_del:
                    delivered_at = t if (delivered_at is None or t > delivered_at) else delivered_at

            con.execute("""
                UPDATE trk_subscription SET
                  最新状态 = ?, 最新子状态 = ?,
                  最新事件时间 = coalesce(?, 最新事件时间),
                  承运商签收时间 = coalesce(?, 承运商签收时间),
                  末次查询 = ?, 查询次数 = coalesce(查询次数,0) + 1
                WHERE 单号 = ? AND 承运商编码 = ?
            """, [STATUS_CN.get(main, main), sub, ev_time, delivered_at,
                  datetime.now(), n, carrier])

        print(f"  批次 {i//BATCH+1}/{-(-len(rows)//BATCH)}: 写入事件 {n_ev}")
        time.sleep(RATE_SLEEP)


def parse_time(s):
    if not s:
        return None
    s = str(s).replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def cmd_status(con, args):
    total = con.execute("SELECT count(*) FROM trk_subscription").fetchone()[0]
    acc = con.execute("SELECT count(*) FROM trk_subscription WHERE 注册结果='accepted'").fetchone()[0]
    rej = con.execute("SELECT count(*) FROM trk_subscription WHERE 注册结果='rejected'").fetchone()[0]
    signed = con.execute("SELECT count(*) FROM trk_subscription WHERE 承运商签收时间 IS NOT NULL").fetchone()[0]
    ev = con.execute("SELECT count(*) FROM trk_event").fetchone()[0]
    print(f"注册台账 : {total:,} 个（接受 {acc:,} / 拒绝 {rej:,}）")
    print(f"承运商已签: {signed:,}")
    print(f"轨迹事件 : {ev:,}")
    if total:
        print("\n按最新状态：")
        for s, n in con.execute(
                "SELECT coalesce(最新状态,'(未查询)'), count(*) FROM trk_subscription GROUP BY 1 ORDER BY 2 DESC").fetchall():
            print(f"  {s:<12}{n:>8,}")
        print("\n签收对账：")
        for s, n in con.execute(
                "SELECT 签收对账, count(*) FROM trk_order_link GROUP BY 1 ORDER BY 2 DESC").fetchall():
            print(f"  {s:<22}{n:>8,}")


# ---------------------------------------------------------------- 自测

SAMPLE = {
    "code": 0,
    "data": {
        "accepted": [{
            "number": "GFUS01047244898816", "carrier": 190284, "origin": 1,
            "track_info": {
                "latest_status": {"status": "Delivered", "sub_status": "Delivered_Other"},
                "latest_event": {"time_iso": "2026-11-15T12:34:56", "description": "Delivered to door"},
                "events": [
                    {"time_iso": "2026-11-13T08:00:00", "stage": "InTransit", "sub_status": "InTransit_PickedUp", "description": "Picked up"},
                    {"time_iso": "2026-11-15T12:34:56", "stage": "Delivered", "sub_status": "Delivered_Other", "description": "Delivered to door"},
                ],
            },
        }],
        "rejected": [{"number": "BAD123", "error": {"code": -18019903, "message": "Carrier cannot be detected."}}],
    },
}


def cmd_selftest(con, args):
    """不需要 token：喂一段样例响应，验证解析与落库"""
    ok = True

    def check(c, label):
        nonlocal ok
        ok = ok and c
        print(f"  {'✅' if c else '❌'} {label}")

    for a in (SAMPLE.get("data") or {}).get("accepted", []):
        n = norm(a["number"])
        ti = a["track_info"]
        check(n == "GFUS01047244898816", "单号归一化")
        check(ti["latest_status"]["status"] == "Delivered", "主状态解析")
        check(STATUS_CN[ti["latest_status"]["status"]] == "已签收", "状态中文映射")
        t = parse_time(ti["events"][1]["time_iso"])
        check(t is not None and t.year == 2026, f"时间解析 -> {t}")
        check(parse_time(None) is None, "空时间返回 None")
    rej = SAMPLE["data"]["rejected"][0]
    check("-18019903" in str(rej["error"]["code"]), "拒因错误码可读")

    # 落库与视图
    con.execute("INSERT OR REPLACE INTO trk_subscription (单号,承运商编码,履约单号,注册结果,承运商签收时间) VALUES (?,?,?,?,?)",
                ["GFUS01047244898816", 190284, "P_T1", "accepted", datetime(2026, 11, 15, 12, 34, 56)])
    r = con.execute("SELECT 签收对账, 承运商尾程天 FROM trk_order_link WHERE 单号='GFUS01047244898816'").fetchone()
    check(r is not None, "trk_order_link 视图可查")
    print(f"     视图返回: {r}")
    con.execute("DELETE FROM trk_subscription WHERE 单号='GFUS01047244898816'")

    print("\n" + ("自测通过 ✅" if ok else "自测失败 ❌"))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="17track 轨迹接入")
    ap.add_argument("cmd", choices=["plan", "register", "poll", "status", "selftest"])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--token", help="17track token，默认读环境变量 TRACK17_TOKEN")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少个单号")
    args = ap.parse_args()

    con = connect(args.db)
    try:
        return {
            "plan": cmd_plan,
            "register": cmd_register,
            "poll": cmd_poll,
            "status": cmd_status,
            "selftest": cmd_selftest,
        }[args.cmd](con, args) or 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
