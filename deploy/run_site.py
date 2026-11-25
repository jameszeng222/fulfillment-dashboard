#!/usr/bin/env python3
"""站点启动入口：python3 deploy/run_site.py

供中枢平台按站点发布时执行。脚本只做三件事：
1. 准备站点运行环境（优先使用本目录 .venv，缺少依赖时按 requirements.txt 安装）；
2. 补齐站点运行需要的非密钥默认值（监听端口、中枢服务地址、OAuth 公开参数）；
3. 以前台进程启动 uvicorn。

密钥（OPS_CLIENT_SECRET、OPS_SITE_MCP_KEY、OPS_SITE_AI_KEY）只从环境变量读取，
平台在受管部署时注入；本脚本不生成、不落盘、不打印任何密钥。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PACKAGES = ("fastapi", "uvicorn", "duckdb", "pandas", "openpyxl")
DEFAULT_ORIGIN = "https://biz.htwig.com:18700"
CALLBACK_PATH = "/ops/auth/callback"
SERVICES = {
    "OPS_SITE_MCP_URL": "https://ops.htwig.com:10000/api/assistant/ai/sites/11/mcp",
    "OPS_SITE_AI_URL": "https://ops.htwig.com:10000/api/assistant/ai/v1",
    "OPS_SITE_SKILLS_URL": "https://ops.htwig.com:10000/api/assistant/ai/sites/11/skills",
}


def log(message: str) -> None:
    print(f"[site] {message}", flush=True)


def missing_packages() -> list[str]:
    return [name for name in PACKAGES if importlib.util.find_spec(name) is None]


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def reexec(argv: list[str]) -> None:
    """Replace this process on POSIX; Windows execv cannot quote spaced paths."""
    if os.name == "nt":
        raise SystemExit(subprocess.call(argv))
    os.execv(argv[0], argv)


def install(python: str) -> None:
    log("安装依赖 requirements.txt")
    subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "-r", str(ROOT / "requirements.txt")], check=True)


def ensure_runtime() -> None:
    """Re-exec inside a prepared interpreter, then verify dependencies."""
    if sys.prefix != str(VENV) and venv_python().exists():
        reexec([str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]])
    if not missing_packages():
        return
    if sys.prefix != str(VENV):
        log("创建站点虚拟环境 .venv")
        if subprocess.run([sys.executable, "-m", "venv", str(VENV)]).returncode == 0 \
                and venv_python().exists():
            reexec([str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]])
        log("无法创建虚拟环境，改用当前 Python 安装依赖")
    install(sys.executable)
    still_missing = missing_packages()
    if still_missing:
        raise SystemExit("缺少依赖：" + ", ".join(still_missing))


def configure() -> None:
    """Fill non-secret defaults. Platform-provided values always win."""
    redirect = os.environ.get("OPS_REDIRECT_URI", "").strip()
    parts = urlsplit(redirect)
    if "OPS_SITE_ORIGIN" not in os.environ or not os.environ["OPS_SITE_ORIGIN"].strip():
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme == "https" and parts.netloc else DEFAULT_ORIGIN
        os.environ["OPS_SITE_ORIGIN"] = origin
    if not redirect:
        os.environ["OPS_REDIRECT_URI"] = os.environ["OPS_SITE_ORIGIN"].rstrip("/") + CALLBACK_PATH
    for name, url in SERVICES.items():
        os.environ.setdefault(name, url)
    os.environ.setdefault("PORT", "18700")
    # 中枢登记的站点访问权即授权范围；首次登录按 OPS_SIGNUP_ROLE 建档。
    os.environ.setdefault("OPS_ALLOW_VIEWER_SIGNUP", "true")
    os.environ.setdefault("OPS_SIGNUP_ROLE", "viewer")
    os.environ.setdefault("OPS_ASSISTANT_ROLES", "admin,logistics,cs,viewer")
    os.environ.setdefault("SESSION_TTL_SEC", "43200")


def init_database() -> None:
    """Create the empty operations schema on first start; never seeds passwords."""
    if (ROOT / "ops.db").is_file():
        return
    log("初始化操作库 ops.db（不创建本地口令账号）")
    subprocess.run([sys.executable, str(ROOT / "init_ops.py"), "--no-seed"], check=True)


def summary() -> None:
    keys = ("OPS_SITE_ORIGIN", "OPS_REDIRECT_URI", "OPS_AUTHORIZE_URL", "OPS_TOKEN_URL", "OPS_USERINFO_URL",
            "OPS_SITE_MCP_URL", "OPS_SITE_AI_URL", "OPS_SITE_SKILLS_URL", "OPS_SIGNUP_ROLE", "OPS_ASSISTANT_ROLES")
    for name in keys:
        log(f"{name}={os.environ.get(name, '')}")
    for name in ("OPS_CLIENT_ID", "OPS_CLIENT_SECRET", "OPS_SITE_MCP_KEY", "OPS_SITE_AI_KEY"):
        log(f"{name}={'已配置' if os.environ.get(name) else '未配置'}")
    log(f"数据文件 履约单.duckdb={'存在' if (ROOT / '履约单.duckdb').is_file() else '缺失'}")


def main() -> None:
    os.chdir(ROOT)
    ensure_runtime()
    if "--check" in sys.argv:
        configure()
        summary()
        return
    configure()
    init_database()
    summary()
    port = os.environ["PORT"]
    log(f"启动服务 0.0.0.0:{port}")
    reexec([sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "0.0.0.0", "--port", port,
            "--proxy-headers", "--forwarded-allow-ips=127.0.0.1",
            "--log-level", "info"])


if __name__ == "__main__":
    main()
