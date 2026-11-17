"""
把 ERP 导出的履约单合并进 履约单.duckdb。

用法:
    python ingest.py 导出.xlsx
    python ingest.py 导出.parquet
    python ingest.py raw/                      # 目录: 合并该目录下所有 .parquet/.xlsx
    python ingest.py 导出.xlsx --dry-run        # 只看会动多少行, 不写库
    python ingest.py 导出.xlsx --batch 2026_11_20   # 指定批次名

合并策略：**按「创建日期」区间整体替换**。
  同一个导出窗口重复导入不会产生重复行，也不需要人工判断该删哪一批；
  代价是如果新导出只覆盖某天的一半，那一天的旧数据会被整个替换掉。

派生列（时间戳、创建日期/月份、时间异常）由本脚本按与清洗时一致的规则重新计算。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import duckdb

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


def load_frames(paths: list[Path]):
    """读入所有文件并纵向合并。parquet 走 duckdb，xlsx 走 pandas。"""
    import pandas as pd

    frames = []
    for p in paths:
        suffix = p.suffix.lower()
        if suffix == ".parquet":
            df = duckdb.connect().execute(
                "SELECT * FROM read_parquet(?)", [str(p)]
            ).df()
        elif suffix in (".xlsx", ".xls"):
            df = pd.read_excel(p, dtype=str)
        elif suffix == ".csv":
            df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
        else:
            print(f"  跳过（不支持的类型）: {p.name}")
            continue
        df["__src__"] = p.name
        print(f"  读入 {p.name}: {len(df):,} 行 × {len(df.columns)} 列")
        frames.append(df)

    if not frames:
        raise SystemExit("没有可读的输入文件")

    out = pd.concat(frames, ignore_index=True, sort=False)
    return out


def collect_inputs(target: Path) -> list[Path]:
    if target.is_dir():
        files = sorted(
            [p for p in target.rglob("*") if p.suffix.lower() in (".parquet", ".xlsx", ".xls", ".csv")]
        )
        if not files:
            raise SystemExit(f"目录里没有可读文件: {target}")
        return files
    if not target.exists():
        raise SystemExit(f"文件不存在: {target}")
    return [target]


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
        existing = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='履约单明细'"
        ).fetchall()}
        existing_ordered = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='履约单明细' ORDER BY ordinal_position"
        ).fetchall()]
        if not existing:
            raise SystemExit("库里找不到 履约单明细 表")

        base_cols = [c for c in existing if c not in DERIVED]
        print(f"目标表可写列 {len(base_cols)} 个（另有 {len(DERIVED)} 个派生列由脚本计算）\n")

        inputs = collect_inputs(Path(args.input))
        df = load_frames(inputs)
        print(f"\n合计 {len(df):,} 行")

        missing = [c for c in CORE if c not in df.columns]
        if missing:
            raise SystemExit(f"缺少必需列: {missing}")

        # 只保留目标表存在的列，其余列丢弃并提示
        usable = [c for c in base_cols if c in df.columns]
        dropped = [c for c in df.columns if c not in usable and c != "__src__"]
        if dropped:
            print(f"输入里有 {len(dropped)} 列不在目标表中，已忽略: {dropped[:8]}{'...' if len(dropped) > 8 else ''}")
        print(f"将写入 {len(usable)} 列")

        # ---- 时间戳派生 ----
        import pandas as pd

        for src, dst in TIME_COLS:
            if src in df.columns:
                df[dst] = pd.to_datetime(df[src].replace("", None), errors="coerce")
            else:
                df[dst] = pd.NaT

        # ---- 清洗：与清洗记录保持同一套规则 ----
        # 「数据日」= 导入时刻（数据不可能包含此刻之后的事件）。
        # 不能用本批数据的最大创建日期：那样会把"6 月下单、9 月才签收"的正常长途单
        # 误判成越界并置空。允许用 --asof 固定，方便复现历史导入。
        now_day = pd.Timestamp(args.asof) if args.asof else pd.Timestamp.now().normalize()
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

        # ---- 批次信息 ----
        batch = args.batch or datetime.now().strftime("%Y_%m_%d_%H_%M")
        df["快照批次"] = batch
        df["filename"] = df["__src__"] if "__src__" in df.columns else ""
        df["源文件"] = ""

        dmin, dmax = df["创建日期"].min(), df["创建日期"].max()
        n_days = df["创建日期"].nunique()
        print(f"\n批次 {batch}")
        print(f"  创建日期范围: {dmin} ~ {dmax}（{n_days} 天）")
        print(f"  履约单 {df['履约单号'].nunique():,} 个")
        print(f"  清洗: 签收越界 {n_out} 行、签收早于签出 {n_bad} 行（已置空并打标）")

        will_del = con.execute(
            "SELECT count(*) FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?", [dmin, dmax]
        ).fetchone()[0]
        print(f"\n合并策略: 删除该日期区间已有 {will_del:,} 行，再插入 {len(df):,} 行")

        if args.dry_run:
            print("\n--dry-run：未写入。")
            _post_check(con, dmin, dmax, len(df), dry=True)
            return 0

        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "DELETE FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?", [dmin, dmax]
            )
            # 目标表里存在、且 DataFrame 里也有的列，一次性写入（含派生列）
            all_cols = [c for c in existing_ordered if c in df.columns]
            con.register("incoming", df[all_cols])
            cols = ", ".join(f'"{c}"' for c in all_cols)
            con.execute(f'INSERT INTO 履约单明细 ({cols}) SELECT {cols} FROM incoming')
            con.unregister("incoming")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        print("\n写入完成。")
        _post_check(con, dmin, dmax, len(df), dry=False)
        return 0
    finally:
        con.close()


def _post_check(con, dmin, dmax, expected, dry: bool):
    total = con.execute("SELECT count(*) FROM 履约单明细").fetchone()[0]
    span = con.execute(
        "SELECT count(*), count(DISTINCT 履约单号) FROM 履约单明细 WHERE 创建日期 BETWEEN ? AND ?",
        [dmin, dmax],
    ).fetchone()
    print(f"  库内总行数: {total:,}")
    print(f"  该区间行数: {span[0]:,}（履约单 {span[1]:,}）")
    if not dry and span[0] != expected:
        print(f"  [警告] 区间行数 {span[0]:,} 与写入 {expected:,} 不一致，检查是否有重复键")


if __name__ == "__main__":
    sys.exit(main())
