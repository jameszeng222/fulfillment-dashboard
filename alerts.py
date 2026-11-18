"""
业务预警引擎 —— 把《物流轨迹预警监控系统 PRD》的规则落到真实数据上。

原则（沿用 PRD）：
  · 17track 官方主状态/子状态**不参与改写**，预警是叠加在最上面的一层判断
  · 同一履约单可命中多条规则，但只输出一个「主预警」，其余记为次预警
  · 每条规则都有明确的排除条件，避免把"还在正常时效内"的单报成异常

已实现（数据支持）：
  R02 超时未签出   履约单生成 >24h 仍未签出
  R03 物流未上网   签出 >2 自然日且无揽收轨迹
  R04 运输超时     已签出未签收，超过该线路 SLA + 2 工作日
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
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
OUT_CSV = ROOT / "out" / "履约预警清单.csv"

# 优先级：数字越小越优先（PRD 顺序，去掉未接入的三条）
RULE_PRIORITY = {
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

RULE_CN = {
    "signout_timeout": "超时未签出",
    "not_online": "物流未上网",
    "transport_timeout": "运输超时",
    "no_update": "物流断更",
    "stagnation": "物流停滞",
    "customs_hold": "海关卡关",
    "delivery_failure": "派送异常",
    "returning": "包裹退运",
    "carrier_exception": "其他异常",
}

RULE_SEVERITY = {
    "signout_timeout": "高",
    "returning": "紧急",
    "carrier_exception": "紧急",
    "delivery_failure": "紧急",
    "customs_hold": "高",
    "no_update": "高",
    "stagnation": "中",
    "not_online": "高",
    "transport_timeout": "高",
}

# 六个一级业务分类（PRD 第 6.4 节）
RULE_GROUP = {
    "signout_timeout": "履约准备异常",
    "not_online": "未上网异常",
    "transport_timeout": "运输异常",
    "no_update": "运输异常",
    "stagnation": "运输异常",
    "customs_hold": "海关卡关",
    "delivery_failure": "派送异常",
    "returning": "包裹退运",
    "carrier_exception": "其他异常",
}

DDL = """
CREATE OR REPLACE TABLE fct_alert (
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


def build(con, asof: str | None):
    load_calendar(con)

    dmax = con.execute("SELECT max(创建日期) FROM 履约单明细").fetchone()[0]
    asof = asof or str(dmax)
    print(f"评估基准日: {asof}（数据最新下单日）")

    # ---- 订单底表（一单一行）+ 轨迹 + 线路 SLA ----
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE base AS
    WITH o AS (
        SELECT 履约单号,
               any_value(出库单状态) 出库单状态,
               any_value(物流服务商) 物流服务商,
               any_value(仓库代码)   仓库代码,
               any_value(目的国家英文名称) 目的国,
               upper(regexp_replace(coalesce(any_value(快递单号),''),'[^A-Za-z0-9]','','g')) 快递单号,
               min(创建时间_ts) 创建时间,
               min(签出时间_ts) 签出时间,
               any_value(签收时间_ts) 签收时间,
               min(创建日期) 创建日期
        FROM 履约单明细 GROUP BY 履约单号
    ),
    -- 线路 SLA：历史已签收单的 P90 尾程天数
    sla AS (
        SELECT 仓库代码, 物流服务商, 目的国,
               quantile_cont(date_diff('minute', 签出时间, 签收时间)/1440.0, 0.9) p90,
               count(*) n
        FROM o WHERE 签收时间 IS NOT NULL AND 签出时间 IS NOT NULL AND 签收时间 > 签出时间
        GROUP BY 1,2,3
    ),
    g AS (SELECT quantile_cont(date_diff('minute', 签出时间, 签收时间)/1440.0, 0.9) p90
          FROM o WHERE 签收时间 IS NOT NULL AND 签出时间 IS NOT NULL AND 签收时间 > 签出时间)
    SELECT o.*,
           t.状态 AS 轨迹状态, t.子状态 AS 轨迹子状态, t.最新事件时间, t.停滞天数,
           t.签收时间 AS 承运商签收时间,
           coalesce(sla.p90, g.p90, 7) AS 线路P90天,
           date_diff('day', o.创建日期, DATE '{asof}') AS 下单已过天,
           CASE WHEN o.签出时间 IS NOT NULL
                THEN date_diff('day', o.签出时间::DATE, DATE '{asof}') END AS 签出已过天,
           CASE WHEN t.最新事件时间 IS NOT NULL
                THEN (SELECT wd FROM cal_idx WHERE d = t.最新事件时间::DATE) END AS 事件wd,
           CASE WHEN o.签出时间 IS NOT NULL
                THEN (SELECT wd FROM cal_idx WHERE d = o.签出时间::DATE) END AS 签出wd,
           (SELECT wd FROM cal_idx WHERE d = DATE '{asof}') AS 基准wd
    FROM o
    LEFT JOIN trk_status t ON t.单号 = o.快递单号
    LEFT JOIN sla ON sla.仓库代码=o.仓库代码 AND sla.物流服务商=o.物流服务商 AND sla.目的国=o.目的国
    CROSS JOIN g
    """)

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
           round(下单已过天 * 24.0, 1) AS 天数
    FROM b
    WHERE 签出时间 IS NULL AND 签出已过天 IS NULL AND 下单已过天 >= 1

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
      AND (基准wd - 事件wd) > 3

    UNION ALL
    -- R05 物流断更：运输中，最后一条有效轨迹 >3 工作日
    SELECT 履约单号, 'no_update', coalesce(轨迹状态,''), (基准wd - 事件wd)
    FROM b
    WHERE 轨迹状态 = 'InTransit' AND 事件wd IS NOT NULL AND (基准wd - 事件wd) > 3
      AND 签收时间 IS NULL

    UNION ALL
    -- R06 物流停滞：已到目的地但不动 >3 工作日（子状态 Arrival/Other 且停滞）
    SELECT 履约单号, 'stagnation', coalesce(轨迹子状态,''), 停滞天数
    FROM b
    WHERE 轨迹子状态 = 'InTransit_Arrival' AND 停滞天数 > 3 AND 签收时间 IS NULL

    UNION ALL
    -- R03 物流未上网：签出 >2 自然日，且**有轨迹但停在"仅收到信息/查无此单"**
    -- 注意：完全没有轨迹数据的单不算未上网——那可能只是单号没同步进 17track，
    -- 无法区分"没上网"和"没数据"，硬报会制造 3 万条假预警。
    -- 这类单单独计入数据健康（无轨迹数据），不进入业务预警。
    SELECT 履约单号, 'not_online', coalesce(轨迹状态,''), 签出已过天
    FROM b
    WHERE 签收时间 IS NULL AND 签出时间 IS NOT NULL AND 签出已过天 > 2
      AND 轨迹状态 IN ('InfoReceived','NotFound')

    UNION ALL
    -- R04 运输超时：已签出未签收、17track 也没显示已签收，且超过线路 P90 + 2 工作日
    SELECT 履约单号, 'transport_timeout', '线路P90 ' || round(线路P90天,1) || '天', 签出已过天
    FROM b
    WHERE 签收时间 IS NULL AND 签出时间 IS NOT NULL
      AND coalesce(轨迹状态,'') <> 'Delivered'
      AND coalesce(承运商签收时间, 签收时间) IS NULL
      AND 签出已过天 > 线路P90天 + 2
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
    FROM hits h
    """)

    con.execute(DDL)
    con.execute(f"""
    INSERT INTO fct_alert
    SELECT b.履约单号, r.规则,
           CASE r.规则 {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in RULE_CN.items())} ELSE r.规则 END,
           CASE r.规则 {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in RULE_GROUP.items())} ELSE '其他异常' END,
           CASE r.规则 {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in RULE_SEVERITY.items())} ELSE '中' END,
           r.rn = 1,
           r.证据, r.天数,
           b.出库单状态, b.物流服务商, b.仓库代码, b.目的国, b.快递单号,
           b.轨迹状态, b.轨迹子状态, b.创建日期, b.签出时间::DATE, DATE '{asof}'
    FROM ranked r JOIN base b USING (履约单号)
    ORDER BY r.rn, r.prio
    """)

    total = con.execute("SELECT count(*) FROM fct_alert").fetchone()[0]
    main_cnt = con.execute("SELECT count(*) FROM fct_alert WHERE 是否主预警").fetchone()[0]
    print(f"写入 fct_alert: {total:,} 行（主预警 {main_cnt:,} 单）")


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
