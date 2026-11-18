"""
ERP 导出文件 → 本地数据库 → 看板，一条龙同步。

用法：
    python sync.py                      # 扫默认目录，导入所有新文件并重建看板
    python sync.py --dry-run            # 只看会导入哪些，不动库
    python sync.py --max 1              # 本次最多处理 1 个文件（试跑用）
    python sync.py --dir D:\导出        # 指定目录（可重复/逗号分隔）
    python sync.py --no-build           # 只入库，不重建看板
    python sync.py --push               # 重建后自动 git commit + push（部署）

默认扫描目录：
    %USERPROFILE%\\Downloads
    %USERPROFILE%\\Desktop\\履约单2026   （存在才扫）

去重：按文件**内容 sha256** 记入 sync_ledger 表。同一个文件换个名字再放一次不会重复导入；
真·重导请从 ledger 里删掉那一行。

合并语义沿用 ingest.py：默认按主键（履约单号+sku+批次码）upsert，
部分导出不会误删同期的其它单；确需整段替换时加 --mode replace。
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

import ingest

ROOT = Path(__file__).resolve().parent
DB = ROOT / "履约单.duckdb"
DEFAULT_DIRS = [
    Path.home() / "Downloads",
    Path.home() / "Desktop" / "履约单2026",
]

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS sync_ledger (
    sha256       VARCHAR PRIMARY KEY,
    filename     VARCHAR,
    size_bytes   BIGINT,
    rows         BIGINT,
    orders       BIGINT,
    创建日起     DATE,
    创建日止     DATE,
    batch        VARCHAR,
    ingested_at  TIMESTAMP
)
"""


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def ensure_ledger(con):
    con.execute(LEDGER_DDL)


def already_ingested(con, sha: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sync_ledger WHERE sha256 = ?", [sha]).fetchone() is not None


def find_candidates(dirs: list[Path], pattern: str) -> list[Path]:
    out = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.glob(pattern):
            if p.is_file() and p.suffix.lower() in ingest.SUPPORTED:
                out.append(p)
    # 同一个文件可能在两个目录里都有，按大小+名字初筛，真正的判重靠 sha256
    seen, uniq = set(), []
    for p in sorted(out, key=lambda x: x.stat().st_mtime):
        key = (p.name, p.stat().st_size)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def run_script(name: str) -> bool:
    print(f"\n>>> {name}")
    r = subprocess.run([sys.executable, str(ROOT / name)], cwd=ROOT)
    if r.returncode != 0:
        print(f"[失败] {name} 退出码 {r.returncode}")
        return False
    return True


def git_push(commit_msg: str) -> bool:
    cmds = [
        ["git", "add", "-A"],
        ["git", "commit", "-m", commit_msg],
        ["git", "push"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            blob = ((r.stdout or "") + (r.stderr or ""))
            if "nothing to commit" in blob or "无文件要提交" in blob:
                print("  没有变更需要提交")
                return True
            print(f"  [失败] {' '.join(c[:2])}: {blob.strip()[:300]}")
            return False
    print("  已推送，Cloudflare 会自动重建")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="ERP 导出 → 数据库 → 看板 同步")
    ap.add_argument("--dir", action="append", help="扫描目录，可多次指定或用逗号分隔")
    ap.add_argument("--pattern", default="履约单数据_2026_*.xls*",
                    help="文件名模式，默认只扫 2026 年的导出")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true",
                    help="把当前已存在的文件登记为「库里已有」而不导入，用于第一次上线，之后只处理新文件")
    ap.add_argument("--max", type=int, default=0, help="本次最多处理几个文件，0=不限")
    ap.add_argument("--no-build", action="store_true", help="不重建看板")
    ap.add_argument("--push", action="store_true", help="重建后自动提交并推送")
    ap.add_argument("--mode", choices=["upsert", "replace"], default="upsert",
                    help="合并方式，默认 upsert（按主键覆盖，部分导出也不会误删）")
    args = ap.parse_args()

    dirs = DEFAULT_DIRS
    if args.dir:
        dirs = []
        for d in args.dir:
            for part in d.split(","):
                part = part.strip()
                if part:
                    dirs.append(Path(part).expanduser())

    print("扫描目录：")
    for d in dirs:
        print(f"  {'✓' if d.exists() else '×'} {d}")

    cands = find_candidates(dirs, args.pattern)
    print(f"\n找到 {len(cands)} 个候选文件")

    con = duckdb.connect(args.db)
    try:
        ensure_ledger(con)

        if args.seed:
            return _seed(con, cands)

        todo = []
        for p in cands:
            sha = sha256_of(p)
            if already_ingested(con, sha):
                continue
            todo.append((p, sha))

        print(f"其中 {len(todo)} 个是新的（未导入过）")
        if not todo:
            print("\n没有需要导入的文件。")
            return 0

        if args.max > 0:
            todo = todo[: args.max]

        for p, sha in todo:
            print(f"\n{'='*56}")
            print(f"处理: {p.name}  ({p.stat().st_size/1024/1024:.1f} MB)")
            t0 = time.time()
            df = ingest.read_one(p)
            batch = p.stem
            try:
                stats = ingest.ingest_df(con, df, batch=batch, dry_run=args.dry_run, mode=args.mode)
            except SystemExit as e:
                print(f"  [跳过] {e}")
                continue

            if args.dry_run:
                continue

            con.execute(
                "INSERT OR REPLACE INTO sync_ledger "
                "(sha256, filename, size_bytes, rows, orders, 创建日起, 创建日止, batch, ingested_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [sha, p.name, p.stat().st_size, stats["rows"], stats["orders"],
                 stats["dmin"], stats["dmax"], batch, datetime.now()],
            )
            print(f"  用时 {time.time()-t0:.0f} 秒")

        if args.dry_run:
            print("\n--dry-run：未写入任何数据。")
            _summary(con)
            return 0

        _summary(con)
    finally:
        con.close()

    if args.no_build:
        print("\n已跳过看板重建（--no-build）")
        return 0

    ok = run_script("build_cube.py") and run_script("assemble.py")
    if not ok:
        print("\n[警告] 看板重建失败，但数据已入库。修好后重跑 build_cube.py / assemble.py。")
        return 1

    if args.push:
        git_push(f"数据同步 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    else:
        print("\n看板已重建。要发布到线上，执行：git add -A && git commit -m 数据同步 && git push")

    print("\n完成。")
    return 0


def _summary(con):
    n = con.execute("SELECT count(*) FROM sync_ledger").fetchone()[0]
    rows = con.execute("SELECT count(*) FROM 履约单明细").fetchone()[0]
    last = con.execute(
        "SELECT filename, ingested_at FROM sync_ledger ORDER BY ingested_at DESC LIMIT 1").fetchone()
    print(f"\n导入记录: {n} 个文件")
    print(f"库内总行数: {rows:,}")
    if last:
        print(f"最近导入: {last[0]} @ {last[1]}")


def _seed(con, cands: list[Path]) -> int:
    """
    把当前已存在的文件登记为「库里已有数据」，不导入。
    用于第一次上线：库里已经有这些 2026 数据了，不需要重跑 1.2GB 的 Excel。
    """
    n_new = 0
    for p in cands:
        sha = sha256_of(p)
        if already_ingested(con, sha):
            continue
        con.execute(
            "INSERT OR REPLACE INTO sync_ledger "
            "(sha256, filename, size_bytes, rows, orders, batch, ingested_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [sha, p.name, p.stat().st_size, None, None, "(上线前已在库中)", datetime.now()],
        )
        n_new += 1
    print(f"\n已登记 {n_new} 个既有文件（不导入数据）")
    print("之后放进目录的新文件才会被处理。")
    _summary(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
