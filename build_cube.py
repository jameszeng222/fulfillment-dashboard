"""
把 履约单.duckdb 聚合成看板用的多维立方体。

筛选维度: 月 / 仓库 / 平台 / 物流商(后台) / 国家
所以每个立方体都带 mi wi pi ci ni 五个索引, 再叠加各自的展示维度。

平台用 reference/平台对照表.xlsx 把 PC 编码换成正式名称。

所有度量都是可加和的整数(单量/件数/求和/计数), 平均值在 JS 端用 和/数 现算,
这样任意筛选组合下加减都不会有精度损失。

立方体:
  day     日 × 仓 × 平台 × 物流商 × 国家                -> KPI / 趋势 / 仓库·平台·物流商·国家 排行
  ccar    月 × 仓 × 平台 × 物流商 × 国家 × C端承运商    -> C端显示物流服务商看板
  status  月 × 仓 × 平台 × 物流商 × 国家 × 出库单状态
  aging   月 × 仓 × 平台 × 物流商 × 国家 × 仓内时效档

口径(与原看板逐一核对):
  单量    = 履约单号去重                       1,325,618
  件数    = sum(数量)                          2,095,757
  仓内    = 单级 (创建->签出) 分钟, 对 >0 取均值           24.08h
  物流    = 单级 (签出->签收) 分钟, 对 >0 取均值           3.58d
  签收率  = 有签收时间的单 / 总单                          93.1%
(平均计费重已按需求移除; 该字段 49.6% 未填写)
"""
import duckdb
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
MAP_XLSX = ROOT / "reference" / "平台对照表.xlsx"
OUT = ROOT / "dashboard_src" / "cube.json"

ABANDS = ["12h内", "12-24h", "24-48h", "48-72h", "72h以上", "未签出"]

# C 端承运商名称归一: 大小写不一致、拼写错误、近似重复
CCAR_NORM = """
CASE upper(trim("C端显示物流服务商名称"))
  WHEN 'FEDEX'                THEN 'FedEx'
  WHEN 'FEDEX SMARTPOST'      THEN 'FedEx SmartPost'
  WHEN 'OFF-LINE LOGISTICS'   THEN 'Off-Line Logistics'
  WHEN 'OFFLINE LOGISTICS'    THEN 'Off-Line Logistics'
  WHEN 'UPS MAIL LNNOVATIONS' THEN 'UPS Mail Innovations'
  WHEN 'AMAZON'               THEN 'Amazon Logistics'
  WHEN '3PE'                  THEN '3PE'
  ELSE nullif(trim("C端显示物流服务商名称"), '')
END
"""


def load_platform_map() -> dict:
    """平台编码 -> 平台名称"""
    if not MAP_XLSX.exists():
        print(f"[警告] 找不到 {MAP_XLSX.name}, 平台将显示原始编码", file=sys.stderr)
        return {}
    import openpyxl
    ws = openpyxl.load_workbook(MAP_XLSX, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    mp = {}
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        code = str(r[0]).strip()
        name = str(r[1]).strip() if len(r) > 1 and r[1] else code
        mp[code] = name
    return mp


PMAP = load_platform_map()
print(f"平台对照表: {len(PMAP)} 条")

con = duckdb.connect(str(DB), read_only=True)

# 用一张临时表做映射, 避免把长 SQL 拼进主查询
con.execute("CREATE OR REPLACE TEMP TABLE pfmap(code VARCHAR, name VARCHAR)")
if PMAP:
    con.executemany("INSERT INTO pfmap VALUES (?, ?)", list(PMAP.items()))

raw_pfs = [r[0] for r in con.execute(
    "SELECT DISTINCT 平台 FROM 履约单明细 ORDER BY 1").fetchall()]
unmapped = [c for c in raw_pfs if c not in PMAP]
if unmapped:
    print(f"[警告] 这些平台编码不在对照表里, 将显示原编码: {unmapped}", file=sys.stderr)

con.execute(f"""
CREATE OR REPLACE TEMP TABLE o2 AS
WITH o AS (
  SELECT 履约单号,
         创建月份                     AS ym,
         仓库代码                     AS wh,
         coalesce(m.name, 平台)       AS pf,
         coalesce(nullif(物流服务商,''), '(未分配)')            AS cr,
         coalesce(nullif(trim(目的国家英文名称),''), '(未知)')   AS cn,
         any_value(目的国家中文名称)   AS cn_zh,
         any_value(coalesce({CCAR_NORM}, '(未填)'))              AS ccr,
         出库单状态                   AS st,
         strftime(创建日期,'%Y-%m-%d') AS d,
         sum(数量)                    AS items,
         date_diff('minute', min(创建时间_ts), min(签出时间_ts)) AS hm,
         date_diff('minute', min(签出时间_ts), min(签收时间_ts)) AS tm,
         max(CASE WHEN 签收时间_ts IS NOT NULL THEN 1 ELSE 0 END) AS sg
  FROM 履约单明细
  LEFT JOIN pfmap m ON m.code = 履约单明细.平台
  GROUP BY 履约单号, 创建月份, 仓库代码, 平台, m.name, 物流服务商,
           目的国家英文名称, 出库单状态, 创建日期
)
SELECT *,
  CASE WHEN hm IS NULL OR hm <= 0 THEN NULL ELSE hm END AS h,
  CASE WHEN tm IS NULL OR tm <= 0 THEN NULL ELSE tm END AS t,
  CASE WHEN hm IS NULL OR hm <= 0 THEN '未签出'
       WHEN hm <= 720  THEN '12h内'
       WHEN hm <= 1440 THEN '12-24h'
       WHEN hm <= 2880 THEN '24-48h'
       WHEN hm <= 4320 THEN '48-72h'
       ELSE '72h以上' END AS ab
FROM o
""")

MEAS = """count(*) AS n, sum(items)::BIGINT AS items,
          coalesce(sum(h),0)::BIGINT AS hS, count(h)::BIGINT AS hC,
          coalesce(sum(t),0)::BIGINT AS tS, count(t)::BIGINT AS tC,
          sum(sg)::BIGINT AS sg"""


def col(sql):
    return [r[0] for r in con.execute(sql).fetchall()]


months = col("SELECT ym FROM o2 GROUP BY 1 ORDER BY 1")
whs    = col("SELECT wh FROM o2 GROUP BY 1 ORDER BY count(*) DESC")
pfs    = col("SELECT pf FROM o2 GROUP BY 1 ORDER BY count(*) DESC")
crs    = col("SELECT cr FROM o2 GROUP BY 1 ORDER BY count(*) DESC")
days   = col("SELECT d  FROM o2 GROUP BY 1 ORDER BY 1")
ccars  = col("SELECT ccr FROM o2 GROUP BY 1 ORDER BY count(*) DESC")
stats  = col("SELECT st FROM o2 GROUP BY 1 ORDER BY count(*) DESC")
ctry   = con.execute(
    "SELECT cn, max(cn_zh), count(*) FROM o2 GROUP BY 1 ORDER BY 3 DESC").fetchall()

I = {name: {v: i for i, v in enumerate(vals)} for name, vals in [
    ("m", months), ("w", whs), ("p", pfs), ("c", crs), ("d", days),
    ("k", [r[0] for r in ctry]), ("x", ccars), ("s", stats), ("ab", ABANDS)]}

raw_day = con.execute(f"SELECT d, wh, pf, cr, cn, {MEAS} FROM o2 GROUP BY 1,2,3,4,5").df()
raw_ccar = con.execute("""
    SELECT ym, wh, pf, cr, cn, ccr,
           count(*)::BIGINT n,
           coalesce(sum(t),0)::BIGINT tS, count(t)::BIGINT tC
    FROM o2 GROUP BY 1,2,3,4,5,6""").df()
raw_stat = con.execute(
    "SELECT ym, wh, pf, cr, cn, st, count(*)::BIGINT n FROM o2 GROUP BY 1,2,3,4,5,6").df()
raw_ag = con.execute(
    "SELECT ym, wh, pf, cr, cn, ab, count(*)::BIGINT n FROM o2 GROUP BY 1,2,3,4,5,6").df()


def to_flat(df, keymaps):
    out = []
    for tup in df.itertuples(index=False, name=None):
        k = len(keymaps)
        for j in range(k):
            out.append(keymaps[j][tup[j]])
        for v in tup[k:]:
            out.append(int(v))
    return out


day    = to_flat(raw_day,  [I["d"], I["w"], I["p"], I["c"], I["k"]])
ccar   = to_flat(raw_ccar, [I["m"], I["w"], I["p"], I["c"], I["k"], I["x"]])
status = to_flat(raw_stat, [I["m"], I["w"], I["p"], I["c"], I["k"], I["s"]])
aging  = to_flat(raw_ag,   [I["m"], I["w"], I["p"], I["c"], I["k"], I["ab"]])

tot = con.execute(f"SELECT {MEAS} FROM o2").df().iloc[0]
cov = con.execute(
    "SELECT strftime(创建日期,'%Y-%m-%d') d, count(DISTINCT 履约单号) n "
    "FROM 履约单明细 GROUP BY 1 ORDER BY 1").df()
dm = con.execute("SELECT DISTINCT d, ym FROM o2 ORDER BY 1").df()

payload = {
    "meta": {
        "start": days[0], "end": days[-1],
        "orders": int(tot["n"]),
        "daysTotal": len(days), "daysCovered": len(days), "daysMissing": 0,
        "generated": days[-1],
    },
    "dims": {
        "months": months, "whs": whs, "pfs": pfs, "crs": crs, "days": days,
        "dayMonth": [I["m"][r] for r in dm["ym"]],
        "countries": [r[0] for r in ctry],
        "countryZH": {r[0]: (r[1] or r[0]) for r in ctry},
        "ccars": ccars,
        "statuses": stats, "abands": ABANDS,
    },
    "day": day, "ccar": ccar, "status": status, "aging": aging,
    "coverage": [[r[0], int(r[1])] for r in cov.itertuples(index=False, name=None)],
}

OUT.parent.mkdir(exist_ok=True)
txt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
OUT.write_text(txt, encoding="utf-8")

print(f"维度: 月{len(months)} 仓{len(whs)} 平台{len(pfs)} 物流商{len(crs)} "
      f"国家{len(ctry)} C端{len(ccars)} 日{len(days)}")
print(f"立方体行数: day {len(raw_day):>6}  ccar {len(raw_ccar):>5}  "
      f"status {len(raw_stat):>5}  aging {len(raw_ag):>5}")
print(f"总单量 {int(tot['n']):,}   件数 {int(tot['items']):,}")
print(f"仓内 {tot['hS']/tot['hC']/60:.2f}h   物流 {tot['tS']/tot['tC']/1440:.2f}d   "
      f"签收 {100*tot['sg']/tot['n']:.1f}%")
print("目标  24.08h            3.58d              93.1%")
print(f"cube.json {len(txt)/1024:.0f} KB")
