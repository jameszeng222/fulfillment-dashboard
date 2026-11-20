"""Run the integrated fulfillment console on the local computer."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from fastapi.responses import RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from app import core
from app.main import app


def main():
    parser = argparse.ArgumentParser(description="启动本机履约监控系统预览")
    parser.add_argument("--data-dir", type=Path, required=True, help="包含履约单.duckdb 和 ops.db 的目录")
    parser.add_argument("--facts-file", type=Path, help="用于本机预览的独立履约数据库")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    source = args.data_dir.resolve()
    source_ops = source / "ops.db"
    core.DUCK_DB = args.facts_file.resolve() if args.facts_file else source / "履约单.duckdb"
    core.OPS_DB = Path(__file__).resolve().parent / "ops.db"
    if not core.DUCK_DB.is_file() or not source_ops.is_file():
        parser.error("数据目录需要包含履约单.duckdb 和 ops.db")
    if source_ops == core.OPS_DB:
        parser.error("请选择单独的源数据目录，预览操作库将保存在当前项目下")

    if not core.OPS_DB.exists():
        with closing(sqlite3.connect(source_ops.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(core.OPS_DB)) as copy:
                origin.backup(copy)
                copy.execute("DELETE FROM sessions")
                copy.commit()
    core.SESSION_COOKIE = "fd_local_preview"
    os.environ.setdefault("FULFILLMENT_OPEN_TEST", "true")

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/preview", include_in_schema=False)
    def open_preview():
        return RedirectResponse("/", status_code=303)

    print(f"本地预览 http://127.0.0.1:{args.port}/", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
