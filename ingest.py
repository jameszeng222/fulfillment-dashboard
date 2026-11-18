"""
把 ERP 导出的履约单合并进 履约单.duckdb。

命令行用法：
    python ingest.py 导出.xlsx
    python ingest.py 导出.parquet
    python ingest.py raw/                      # 目录: 合并该目录下所有 .parquet/.xlsx/.csv
    python ingest.py 导出.xlsx --dry-run        # 只看会动多少行, 不写库
    python ingest.py 导出.xlsx --batch 2026_11_20
    python ingest.py 导出.xlsx --asof 2026-11-16   # 固定「数据日」，用于复现历史导入

合并策略：**按「创建日期」区间整体替换**。
同一个导出窗口重复导入不会产生重复行；代价是新导出只覆盖某天的一部分时，
那一天的旧数据会被整个替换掉。

派生列（时间戳、创建日期/月份、时间异常）由本脚本按与清洗时一致的规则重新计算。
本模块的 `load_input` / `ingest_df` 也被 sync.py 复用。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"

# 这 12 列由脚本计算，输入文件里不需要有
DERIVED = {
    "创建时间_ts", "支付时间_ts", "分配时间_ts", "打包时间_ts",
    "签出时间_ts", "签收时间_ts", "时间异常",
    "创建日期", "创建月份", "filename", "源文件", "快照批次",
}

# 最小必需列：缺了这些没法做时效分析，直接拒绝
CORE = [
    "履约单号", "出库单号", "平台", "仓库代码", "sku", "数量",
    "创建时间", "出库单状态", "物流服务商", "快递单号", "目的国家英文名称",
]

TIME_COLS = [
    ("创建时间", "创建时间_ts"),
    ("支付时间", "支付时间_ts"),
    ("分配时间", "分配时间_ts"),
    ("打包时间", "打包时间_ts"),
    ("签出时间", "签出时间_ts"),
    ("签收时间", "签收时间_ts"),
]

SUPPORTED = (".xlsx", ".xls", ".parquet", ".csv")


# ---------------------------------------------------------------- 读取

def read_one(path: Path) -> pd.DataFrame:
    """读单个文件。xlsx 优先用 calamine（比 openpyxl 快约 8 倍），没装则回退。"""
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df = duckdb.connect().execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
    elif suffix in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(path, dtype=str, engine="calamine")
        except Exception:
            df = pd.read_excel(path, dtype=str)
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    else:
        raise ValueError(f"不支持的文件类型: {path.suffix}")
    df["__src__"] = path.name
    return df


def collect_inputs(target: Path) -> list[Path]:
    if target.is_dir():
        files = sorted(p for p in target.rglob("*") if p.suffix.lower() in SUPPORTED)
        if not files:
            raise SystemExit(f"目录里没有可读文件: {target}")
        return files
    if not target.exists():
        raise SystemExit(f"文件不存在: {target}")
    return [target]


def load_input(target: Path, verbose: bool = True) -> pd.DataFrame:
    frames = []
    for p in collect_inputs(target):
        df = read_one(p)
        if verbose:
            print(f"  读入 {p.name}: {len(df):,} 行 × {len(df.columns)} 列")
        frames.append(df)
    if not frames:
        raise SystemExit("没有可读的输入文件")
    return pd.concat(frames, ignore_index=True, sort=False)


# ---------------------------------------------------------------- 清洗与合并

def ingest_df(con, df: pd.DataFrame, batch: str | None = None,
              asof: str | None = None, dry_run: bool = False) -> dict:
    """
    把 DataFrame 合并进库。返回统计信息 dict。
    清理规则与历史上的「清洗记录」保持一致。
    """
    existing_ordered = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='履约单明细' ORDER BY ordinal_position").fetchall()]
    if not existing_ordered:
        raise SystemExit("库里找不到 履约单明细 表")
    existing = set(existing_ordered)

    print(f"\n合计 {len(df):,} 行")
    missing = [c for c in CORE if c not in df.columns]
    if missing:
        raise SystemExit(f"缺少必需列: {missing}")

    usable = [c for c in existing_ordered if c in df.columns]
    dropped = [c for c in df.columns if c not in existing and c != "__src__"]
    if dropped:
        print(f"输入里有 {len(dropped)} 列不在目标表中，已忽略: {dropped[:8]}"
              f"{'...' if len(dropped) > 8 else ''}")
    print(f"将写入 {len(usable)} 列")

    # ---- 时间戳派生 ----
    for src, dst in TIME_COLS:
        if src in df.columns:
            df[dst] = pd.to_datetime(df[src].replace("", None), errors="coerce")
        else:
            df[dst] = pd.NaT

    # ---- 清洗 ----
    # 「数据日」= 导入时刻。不能用本批最大创建日期，否则「6 月下单、9 月才签收」
    # 这类正常长途单会被误判越界并置空。
    now_day = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    created_max = df["创建时间_ts"].dt.normalize().max()
    if pd.isna(created_max):
        raise SystemExit("创建时间整列都解析不出来，检查导出格式")
    data_day = max(now_day, created_max)

    bad_sign = df["签收时间_ts"] < df["签出时间_ts"]
    out_of_range = df["签收时间_ts"] > (data_day + pd.Timedelta(days=30))

    df["时间异常"] = None
    df.loc[out_of_range, "时间异常"] = "签收时间越界(晚于数据日)"
    df.loc[bad_sign, "时间异常"] = "签收早于签出"
    n_out, n_bad = int(out_of_range.sum()), int(bad_sign.sum())
    df.loc[out_of_range | bad_sign, "签收时间_ts"] = pd.NaT

    df["创建日期"] = df["创建时间_ts"].dt.date
    if df["创建日期"].isna().any():
        raise SystemExit(f"有 {int(df['创建日期'].isna().sum())} 行创建时间无法解析，先修导出格式")
    df["创建月份"] = pd.to_datetime(df["创建日期"]).dt.strftime("%Y-%m")

    batch = batch or datetime.now().strftime("%Y_%m_%d_%H_%M")
    df["快照批次"] = batch
    df["filename"] = df["__src__"] if "__src__" in df.columns else ""
    df["源文件"] = ""

    dmin, dmax = df["创建日期"].min(), df["创建日期"].max()
    n_days = int(df["创建日期"].nunique())
    print(f"\n批次 {batch}")
    print(f"  创建日期范围: {dmin} ~ {dmax}（{n_days} 天）")
    print(f"  履约单 {int(df['履约单号'].nunique()):,} 个")
    print(f"  清洗: 签收越界 {n_out} 行、签收早于签出 {n_bad} 行（已置空并打标）")

    will_del = con.execute(
        "SELECT count(*) FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?", [dmin, dmax]
    ).fetchone()[0]
    print(f"\n合并策略: 删除该日期区间已有 {will_del:,} 行，再插入 {len(df):,} 行")

    stats = {
        "batch": batch, "rows": len(df), "orders": int(df["履约单号"].nunique()),
        "dmin": str(dmin), "dmax": str(dmax), "days": n_days,
        "deleted": int(will_del), "bad_sign": n_bad, "out_of_range": n_out,
    }

    if dry_run:
        print("\n--dry-run：未写入。")
        return stats

    all_cols = [c for c in existing_ordered if c in df.columns]
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?", [dmin, dmax])
        con.register("incoming", df[all_cols])
        cols = ", ".join(f'"{c}"' for c in all_cols)
        con.execute(f'INSERT INTO 履约单明细 ({cols}) SELECT {cols} FROM incoming')
        con.unregister("incoming")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    total = con.execute("SELECT count(*) FROM 履约单明细").fetchone()[0]
    got = con.execute(
        "SELECT count(*) FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?", [dmin, dmax]
    ).fetchone()[0]
    print("\n写入完成。")
    print(f"  库内总行数: {total:,}")
    print(f"  该区间行数: {got:,}（履约单 "
          f"{con.execute('SELECT count(DISTINCT 履约单号) FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?', [dmin, dmax]).fetchone()[0]:,}）")
    if got != len(df):
        print(f"  [警告] 区间行数 {got:,} 与写入 {len(df):,} 不一致，检查是否有重复键")
    stats["db_total"] = int(total)
    return stats


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="把 ERP 导出的履约单合并进数据库")
    ap.add_argument("input", help="导出文件或目录")
    ap.add_argument("--db", default=str(DB), help="数据库路径")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    ap.add_argument("--batch", help="批次名，默认按当前时间生成")
    ap.add_argument("--asof", help="把「数据日」固定为某天（如 2026-11-16），用于复现历史导入")
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    try:
        df = load_input(Path(args.input))
        ingest_df(con, df, batch=args.batch, asof=args.asof, dry_run=args.dry_run)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
