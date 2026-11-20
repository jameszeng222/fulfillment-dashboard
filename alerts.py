"""
业务预警引擎 —— 把《物流轨迹预警监控系统 PRD》的规则落到真实数据上。

原则（沿用 PRD）：
  · 17track 官方主状态/子状态**不参与改写**，预警是叠加在最上面的一层判断
  · 同一履约单可命中多条规则，但只输出一个「主预警」，其余记为次预警
  · 每条规则都有明确的排除条件，避免把"还在正常时效内"的单报成异常

已实现（数据支持）：
  R02 超时未签出   履约单生成 >24h 仍未签出
  R03 物流未上网   签出 >2 自然日且无揽收轨迹
  R04 运输超时     已签出未签收，超过当前渠道 SLA 加配置的自然日缓冲
  R05 物流断更     有轨迹但最后一条有效轨迹 >3 工作日
  R07 海关卡关     清关中/需补资料，且停滞 >3 工作日
  R08 派送异常     投递失败 / 待自提
  R09 包裹退运     退件中 / 已退回
  R10 其他异常     丢件 / 破损 / 销毁 / 安全 / 取消

未接入（缺数据源，不臆造）：
  R01 履约单报错、商品缺货、拆单异常 —— 需要 ERP 错误码 / 库存 / 渠道分配结果

用法：
    python alerts.py build            # 重算 fct_alert
    python alerts.py build --asof 2026-11-16
    python alerts.py list --rule returning --limit 50
    python alerts.py summary
    python alerts.py export           # 导出 交付给运营的 CSV
"""
from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from app import core, rules
from app.carrier_delivery import DELIVERED_UTC, reporting_facts
from app.channel_timing import online_scan_join, timing_start
from app.order_time import delivery_facts
from app.tracking_archive import sources as tracking_sources
from app.rules import PRIORITY as RULE_PRIORITY, RULES_BY_KEY

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
OUT_CSV = ROOT / "out" / "履约预警清单.csv"

# 名称/级别/分类全部来自 app/rules.py，保证页面文案与判定口径一致
RULE_CN = {k: v["name"] for k, v in RULES_BY_KEY.items()}
RULE_GROUP = {k: v["category"] for k, v in RULES_BY_KEY.items()}

DDL = """
CREATE OR REPLACE TEMP TABLE computed_alerts (
    履约单号      VARCHAR,
    规则          VARCHAR,
    规则中文      VARCHAR,
    业务分类      VARCHAR,
    级别          VARCHAR,
    是否主预警    BOOLEAN,
    证据          VARCHAR,
    天数          DOUBLE,
    出库单状态    VARCHAR,
    物流服务商    VARCHAR,
    仓库代码      VARCHAR,
    目的国        VARCHAR,
    快递单号      VARCHAR,
    轨迹状态      VARCHAR,
    轨迹子状态    VARCHAR,
    创建日期      DATE,
    签出日期      DATE,
    评估基准日    DATE
)
"""


def load_calendar(con):
    """工作日累计序号表：任意两天之间减去序号即得工作日数"""
    con.execute("""
    CREATE OR REPLACE TEMP TABLE cal AS
    SELECT d::DATE AS d,
           CASE WHEN dayofweek(d) BETWEEN 1 AND 5 THEN 1 ELSE 0 END AS is_wd
    FROM generate_series(DATE '2025-01-01', DATE '2027-12-31', INTERVAL 1 DAY) t(d)
    """)
    con.execute("""
    CREATE OR REPLACE TEMP TABLE cal_idx AS
    SELECT d, sum(is_wd) OVER (ORDER BY d) AS wd FROM cal
    """)


def calculate(con, asof: str | None, rule_overrides, sla_overrides):
    load_calendar(con)

    raw_facts = core.active_facts(core.order_facts(con))
    dmax = con.execute(f"SELECT max(创建日期) FROM {raw_facts}").fetchone()[0]
    asof = date.fromisoformat(str(asof or dmax or date.today())).isoformat()
    effective = rules.effective_rules(rule_overrides)
    thresholds = {row["key"]: row["threshold"] for row in effective if "threshold" in row}
    severities = {row["key"]: row["level"] for row in effective}
    enabled = [row["key"] for row in effective if row["enabled"]]
    facts = reporting_facts(con, delivery_facts(con, raw_facts))
    tracking, _, _ = tracking_sources(con)
    slas = rules.current_channel_slas(con, raw_facts, sla_overrides)
    sla_sql, sla_values = rules.sla_table(slas)
    have_online = any(config["basis"] == "online" for config in slas.values())
    scan_join = online_scan_join(con) if have_online else ""
    sla_start = timing_start("target.basis", have_online)
    print(f"评估基准日: {asof}（最新履约单创建日，北京时间）")

    # ---- 订单底表（一单一行）+ 轨迹 + 线路 SLA ----
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE base AS
    WITH target(ch, days, basis) AS ({sla_sql})
    SELECT o.履约单号, o.状态 AS 出库单状态, o.物流商 AS 物流服务商, o.当前渠道,
           o.仓库 AS 仓库代码, o.目的国, o.单号归一 AS 快递单号,
           o.支付时间, o.创建时间, o.签出时间, o.签收时间, o.创建日期,
           t.状态 AS 轨迹状态, t.子状态 AS 轨迹子状态, t.最新事件时间, t.停滞天数,
           o.{DELIVERED_UTC} AS 承运商签收时间,
           coalesce(target.days, {rules.DEFAULT_SLA_DAYS}) AS 承诺天数,
           CASE target.basis WHEN 'paid' THEN '客户下单（支付）' WHEN 'online' THEN '物流上网'
                ELSE '出库签出' END AS SLA起点,
           date_diff('second', ({sla_start}), TIMESTAMP '{asof}') / 86400.0 AS SLA已过天,
           date_diff('day', o.创建日期, DATE '{asof}') AS 创建已过天,
           CASE WHEN o.签出时间 IS NOT NULL
                THEN date_diff('day', o.签出时间::DATE, DATE '{asof}') END AS 签出已过天,
           CASE WHEN t.最新事件时间 IS NOT NULL
                THEN (SELECT wd FROM cal_idx WHERE d = (t.最新事件时间 + INTERVAL '8 hours')::DATE) END AS 事件wd,
           CASE WHEN o.签出时间 IS NOT NULL
                THEN (SELECT wd FROM cal_idx WHERE d = o.签出时间::DATE) END AS 签出wd,
           (SELECT wd FROM cal_idx WHERE d = DATE '{asof}') AS 基准wd
    FROM {facts} o
    LEFT JOIN {tracking} t ON t.单号 = o.单号归一
    LEFT JOIN target ON target.ch=o.当前渠道
    {scan_join}
    """, sla_values)

    n_open = con.execute(
        "SELECT count(*) FROM base WHERE 出库单状态 NOT IN ('已完成','已取消')").fetchone()[0]
    print(f"未结履约单（不含已完成/已取消）: {n_open:,}")

    # ---- 规则判定 ----
    rules_sql = f"""
    CREATE OR REPLACE TEMP TABLE hits AS
    WITH b AS (SELECT * FROM base WHERE 出库单状态 NOT IN ('已完成','已取消'))
    -- R02 超时未签出：生成 >24h 仍未签出
    SELECT 履约单号, 'signout_timeout' AS 规则,
           'FS' AS 证据,
           round(创建已过天, 1) AS 天数
    FROM b
    WHERE 签出时间 IS NULL AND 签出已过天 IS NULL AND 创建已过天 >= {thresholds['signout_timeout']}

    UNION ALL
    -- R09 包裹退运
    SELECT 履约单号, 'returning', coalesce(轨迹子状态,''), 停滞天数
    FROM b WHERE 轨迹子状态 IN ('Exception_Returning','Exception_Returned')

    UNION ALL
    -- R10 其他异常：异常类但没有明确归类（排除退运/拒收/延误/清关）
    SELECT 履约单号, 'carrier_exception', coalesce(轨迹子状态,''), 停滞天数
    FROM b
    WHERE 轨迹状态 = 'Exception'
      AND coalesce(轨迹子状态,'') NOT IN ('Exception_Returning','Exception_Returned',
            'Exception_Rejected','Exception_Delayed')

    UNION ALL
    -- R08 派送异常：投递失败 / 待自提 / 派送前拒收
    SELECT 履约单号, 'delivery_failure', coalesce(轨迹子状态,''), 停滞天数
    FROM b
    WHERE 轨迹状态 IN ('DeliveryFailure','AvailableForPickup')
       OR 轨迹子状态 = 'Exception_Rejected'

    UNION ALL
    -- R07 海关卡关：清关中/需补资料，且停滞 >3 工作日
    SELECT 履约单号, 'customs_hold', coalesce(轨迹子状态,''), 停滞天数
    FROM b
    WHERE 轨迹子状态 IN ('InTransit_CustomsProcessing','InTransit_CustomsRequiringInformation')
      AND (基准wd - 事件wd) > {thresholds['customs_hold']}

    UNION ALL
    -- R05 物流断更：运输中，最后一条有效轨迹 >3 工作日
    SELECT 履约单号, 'no_update', coalesce(轨迹状态,''), (基准wd - 事件wd)
    FROM b
    WHERE 轨迹状态 = 'InTransit' AND 事件wd IS NOT NULL AND (基准wd - 事件wd) > {thresholds['no_update']}
      AND 签收时间 IS NULL

    UNION ALL
    -- R06 物流停滞：已到目的地但不动 >3 工作日（子状态 Arrival/Other 且停滞）
    SELECT 履约单号, 'stagnation', coalesce(轨迹子状态,''), 停滞天数
    FROM b
    WHERE 轨迹子状态 = 'InTransit_Arrival' AND 停滞天数 > {thresholds['stagnation']} AND 签收时间 IS NULL

    UNION ALL
    -- R03 物流未上网：签出 >2 自然日，且**有轨迹但停在"仅收到信息/查无此单"**
    -- 注意：完全没有轨迹数据的单不算未上网——那可能只是单号没同步进 17track，
    -- 无法区分"没上网"和"没数据"，硬报会制造 3 万条假预警。
    -- 这类单单独计入数据健康（无轨迹数据），不进入业务预警。
    SELECT 履约单号, 'not_online', coalesce(轨迹状态,''), 签出已过天
    FROM b
    WHERE 签收时间 IS NULL AND 签出时间 IS NOT NULL AND 签出已过天 > {thresholds['not_online']}
      AND 轨迹状态 IN ('InfoReceived','NotFound')

    UNION ALL
    -- R04 运输超时：从当前渠道承诺的计时起点算起。
    SELECT 履约单号, 'transport_timeout',
           coalesce(当前渠道, '未填写当前渠道') || ' · ' || SLA起点 || ' · SLA ' || 承诺天数 || '天 + ' || {thresholds['transport_timeout']} || '天',
           round(SLA已过天, 1)
    FROM b
    WHERE 签收时间 IS NULL AND 签出时间 IS NOT NULL
      AND coalesce(轨迹状态,'') <> 'Delivered'
      AND 承运商签收时间 IS NULL
      AND SLA已过天 > 承诺天数 + {thresholds['transport_timeout']}
    """
    con.execute(rules_sql)

    n_hits = con.execute("SELECT count(*) FROM hits").fetchone()[0]
    n_orders = con.execute("SELECT count(DISTINCT 履约单号) FROM hits").fetchone()[0]
    print(f"命中 {n_hits:,} 条规则，涉及 {n_orders:,} 个履约单")

    # ---- 组装，选主预警 ----
    prio_case = " ".join(
        f"WHEN '{k}' THEN {v}" for k, v in RULE_PRIORITY.items()
    )
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE ranked AS
    SELECT h.履约单号, h.规则, h.证据, h.天数,
           CASE h.规则 {prio_case} ELSE 99 END AS prio,
           row_number() OVER (PARTITION BY h.履约单号
                              ORDER BY CASE h.规则 {prio_case} ELSE 99 END) AS rn
    FROM hits h WHERE h.规则 IN (SELECT unnest(?::VARCHAR[]))
    """, [enabled])

    con.execute(DDL)
    con.execute(f"""
    INSERT INTO computed_alerts
    SELECT b.履约单号, r.规则,
           CASE r.规则 {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in RULE_CN.items())} ELSE r.规则 END,
           CASE r.规则 {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in RULE_GROUP.items())} ELSE '其他异常' END,
           CASE r.规则 {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in severities.items())} ELSE '中' END,
           r.rn = 1,
           r.证据, r.天数,
           b.出库单状态, b.物流服务商, b.仓库代码, b.目的国, b.快递单号,
           b.轨迹状态, b.轨迹子状态, b.创建日期, b.签出时间::DATE, DATE '{asof}'
    FROM ranked r JOIN base b USING (履约单号)
    ORDER BY r.rn, r.prio
    """)

    return con.execute("SELECT count(*), count(*) FILTER (WHERE 是否主预警) FROM computed_alerts").fetchone()


def build(con, asof: str | None):
    ops_path = ROOT / "ops.db"
    overrides = ({}, {})
    if ops_path.exists():
        with closing(sqlite3.connect(ops_path)) as settings:
            overrides = rules.load_settings(settings)
    total, main_cnt = calculate(con, asof, *overrides)
    con.execute("CREATE OR REPLACE TABLE fct_alert AS SELECT * FROM computed_alerts")
    print(f"写入 fct_alert: {total:,} 行（主预警 {main_cnt:,} 单）")
    write_order_alert(con)
    push_to_ops(con)



def push_to_ops(dcon):
    """把 fct_alert 镜像到 SQLite 的 alerts 表，供 API 直接查询。
    alert_state（处理状态）不受影响 —— 重算预警不会清掉人已经处理过的记录。"""
    ops = ROOT / "ops.db"
    if not ops.exists():
        print("  ops.db 不存在，跳过镜像（先跑 python init_ops.py）")
        return
    with closing(sqlite3.connect(ops, timeout=15)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        with db:
            n = update_alert_mirror(dcon, db, computed=False)
    print(f"  已镜像到 ops.db: {n:,} 条预警")


ALERT_ORDER_FIELDS = {
    "shipped_at": "o.签出时间::VARCHAR",
    "current_channel": "nullif(trim(o.当前渠道),'')",
}


def add_alert_order_fields(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(alerts)")}
    added = [field for field in ALERT_ORDER_FIELDS if columns and field not in columns]
    for field in added:
        db.execute(f"ALTER TABLE alerts ADD COLUMN {field} TEXT")
    return added


def migrate_alert_order_fields():
    """Backfill new fulfillment fields without rebuilding alerts or changing handling state."""
    with core.ops() as db:
        if ALERT_ORDER_FIELDS.keys() <= {row[1] for row in db.execute("PRAGMA table_info(alerts)")}:
            return
        db.execute("BEGIN IMMEDIATE")
        added = add_alert_order_fields(db)
        if not added:
            return
        orders = [row[0] for row in db.execute("SELECT DISTINCT order_no FROM alerts")]
        if not orders:
            return
        with core.duck() as con:
            values = con.execute(f"""
                SELECT {', '.join(ALERT_ORDER_FIELDS[field] for field in added)}, o.履约单号
                FROM {core.order_facts(con)} o WHERE o.履约单号 IN (SELECT unnest(?::VARCHAR[]))
            """, [orders]).fetchall()
        assignments = ", ".join(f"{field}=?" for field in added)
        db.executemany(f"UPDATE alerts SET {assignments} WHERE order_no=?", values)


def update_alert_mirror(dcon, db, *, computed=True):
    table = "computed_alerts" if computed else "fct_alert"
    rows = dcon.execute(f"""
        SELECT a.履约单号, a.规则, a.规则中文, a.业务分类, a.级别, a.是否主预警, a.证据, a.天数,
               a.出库单状态, a.物流服务商, a.仓库代码, a.目的国, a.快递单号,
               a.轨迹状态, a.轨迹子状态, a.创建日期::VARCHAR, a.签出日期::VARCHAR,
               {ALERT_ORDER_FIELDS['shipped_at']}, a.评估基准日::VARCHAR, {ALERT_ORDER_FIELDS['current_channel']}
        FROM {table} a LEFT JOIN {core.order_facts(dcon)} o USING (履约单号)
    """).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    db.execute("DELETE FROM alerts")
    add_alert_order_fields(db)
    db.executemany(
        "INSERT OR REPLACE INTO alerts (order_no,rule,rule_cn,category,level,is_main,evidence,days,"
        "order_status,carrier,warehouse,country,tracking_no,track_status,track_sub,"
        "created_date,shipped_date,shipped_at,base_date,current_channel,synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], r[4], 1 if r[5] else 0, r[6], r[7], r[8], r[9], r[10], r[11], r[12],
          *r[13:], now) for r in rows])
    return len(rows)


def write_order_alert(dcon):
    """把每单的主预警写回事实层，供轨迹监控页按「自定义物流异常」筛选。"""
    dcon.execute("""
        CREATE OR REPLACE TABLE fct_order_alert AS
        SELECT 履约单号,
               any_value(规则) FILTER (WHERE 是否主预警) AS 主预警规则,
               any_value(规则中文) FILTER (WHERE 是否主预警) AS 主预警名称,
               any_value(级别) FILTER (WHERE 是否主预警)   AS 主预警级别,
               count(*)::BIGINT                            AS 预警数
        FROM fct_alert
        GROUP BY 履约单号
    """)
    n = dcon.execute("SELECT count(*) FROM fct_order_alert").fetchone()[0]
    print(f"  fct_order_alert: {n:,} 单")


def summary(con):
    print("\n== 主预警分布（按履约单去重后只看主预警）==")
    print(con.execute("""
        SELECT 规则中文 预警, 级别, count(*) 单量
        FROM fct_alert WHERE 是否主预警 GROUP BY 1,2 ORDER BY 3 DESC""").df().to_string(index=False))

    print("\n== 六个一级业务分类 ==")
    print(con.execute("""
        SELECT 业务分类, count(*) 单量 FROM fct_alert WHERE 是否主预警
        GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))

    print("\n== 按承运商（主预警）==")
    print(con.execute("""
        SELECT 物流服务商, count(*) 单量 FROM fct_alert WHERE 是否主预警
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10""").df().to_string(index=False))

    print("\n== 命中多条规则的分布 ==")
    print(con.execute("""
        SELECT 命中规则数, count(*) 履约单 FROM (
            SELECT 履约单号, count(*) 命中规则数 FROM fct_alert GROUP BY 1)
        GROUP BY 1 ORDER BY 1""").df().to_string(index=False))


def cmd_list(con, args):
    where = ["是否主预警"] if not args.all else ["1=1"]
    if args.rule:
        where.append(f"规则 = '{args.rule}'")
    sql = f"""SELECT 履约单号, 规则中文 预警, 级别, 证据, round(天数,1) 天数,
                     物流服务商, 仓库代码, 目的国, 轨迹状态, 轨迹子状态, 签出日期
              FROM fct_alert WHERE {' AND '.join(where)}
              ORDER BY CASE 级别 WHEN '紧急' THEN 1 WHEN '高' THEN 2 ELSE 3 END, 天数 DESC
              LIMIT {args.limit}"""
    print(con.execute(sql).df().to_string(index=False))


def cmd_export(con, args):
    OUT_CSV.parent.mkdir(exist_ok=True)
    df = con.execute("""
        SELECT 履约单号, 快递单号, 规则中文, 业务分类, 级别, 证据, round(天数,1) 已过天数,
               出库单状态, 物流服务商, 仓库代码, 目的国, 轨迹状态, 轨迹子状态,
               创建日期, 签出日期, 评估基准日
        FROM fct_alert WHERE 是否主预警
        ORDER BY CASE 级别 WHEN '紧急' THEN 1 WHEN '高' THEN 2 ELSE 3 END, 天数 DESC
    """).df()
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"已导出 {len(df):,} 行 -> {OUT_CSV}")


def main() -> int:
    ap = argparse.ArgumentParser(description="业务预警引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--asof"); b.add_argument("--db", default=str(DB))
    s = sub.add_parser("summary"); s.add_argument("--db", default=str(DB))
    l = sub.add_parser("list"); l.add_argument("--rule"); l.add_argument("--limit", type=int, default=30)
    l.add_argument("--all", action="store_true"); l.add_argument("--db", default=str(DB))
    e = sub.add_parser("export"); e.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    try:
        if args.cmd == "build":
            build(con, args.asof)
            summary(con)
        elif args.cmd == "summary":
            summary(con)
        elif args.cmd == "list":
            cmd_list(con, args)
        elif args.cmd == "export":
            cmd_export(con, args)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
