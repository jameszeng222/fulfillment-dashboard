#!/usr/bin/env python3
"""站点启动入口：python3 deploy/run_site.py

供中枢平台按站点发布时执行。脚本只做三件事：
1. 准备站点运行环境（优先使用本目录 .venv，缺少依赖时按 requirements.txt 安装）；
2. 补齐站点运行需要的非密钥默认值（监听端口、中枢服务地址、OAuth 公开参数）；
3. 以前台进程启动 uvicorn。

密钥（OPS_CLIENT_SECRET、OPS_SITE_AI_KEY）只从环境变量读取，
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
sys.path.insert(0, str(ROOT))

from app.datapaths import fact_database, operations_database

VENV = ROOT / ".venv"
PACKAGES = ("fastapi", "uvicorn", "duckdb", "pandas", "openpyxl")
DEFAULT_ORIGIN = "https://biz.htwig.com:18700"
CALLBACK_PATH = "/ops/auth/callback"
SERVICES = {
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
    if not os.environ.get("OPS_DATA_DIR", "").strip():
        os.environ["OPS_DATA_DIR"] = str(ROOT / "data")
    # The managed site requires real sessions. Local preview flags must not carry over.
    os.environ.update(FULFILLMENT_HUB_SITE="true", FULFILLMENT_OPEN_TEST="false",
                      FULFILLMENT_PUBLIC_PREVIEW="false", FULFILLMENT_CLOUD_TICKETS="false",
                      OPS_LOCAL_LOGIN="false")
    # 中枢登记的站点访问权即授权范围；首次登录按 OPS_SIGNUP_ROLE 建档。
    os.environ.setdefault("OPS_ALLOW_VIEWER_SIGNUP", "true")
    os.environ.setdefault("OPS_SIGNUP_ROLE", "viewer")
    os.environ.setdefault("OPS_ASSISTANT_ROLES", "admin,logistics,cs,viewer")
    os.environ.setdefault("SESSION_TTL_SEC", "43200")


def init_database() -> None:
    """Create an empty operations DB on first start; never overwrite an existing DB."""
    path = operations_database()
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    log("初始化操作库 ops.db（不创建本地口令账号）")
    subprocess.run([sys.executable, str(ROOT / "init_ops.py"), "--db", str(path), "--no-seed"], check=True)


def tls_args() -> list[str]:
    # Select only this site's injected pair. Never search for another site's keys.
    cert = os.environ.get("ICS_TLS_CERT_FILE", "").strip()
    key = os.environ.get("ICS_TLS_KEY_FILE", "").strip()
    if not cert or not key or not Path(cert).is_file() or not Path(key).is_file():
        raise SystemExit("缺少平台注入的 TLS 证书配置，停止启动（不降级为 HTTP）。")
    return ["--ssl-certfile", cert, "--ssl-keyfile", key]


def launch_args() -> list[str]:
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
            "--port", os.environ["PORT"], "--no-proxy-headers", "--no-access-log",
            "--log-level", "info", *tls_args()]


def summary() -> None:
    from app.ops_auth import enabled

    log(f"中枢登录={'已配置' if enabled() else '未配置或回调不匹配'}")
    log(f"事实库={'存在' if fact_database().is_file() else '缺失'}")
    log(f"操作库={'存在' if operations_database().is_file() else '缺失'}")
    log(f"助手 AI 配置={'存在' if os.environ.get('OPS_SITE_AI_KEY') else '未配置'}")
    try:
        tls_args()
    except SystemExit:
        log("平台 TLS 配置缺失")
    else:
        log("平台 TLS 文件存在（尚未验证 HTTPS）")


def main() -> None:
    os.chdir(ROOT)
    ensure_runtime()
    configure()
    summary()
    if "--check" in sys.argv:
        return
    args = launch_args()
    from app.ops_auth import enabled

    if not enabled():
        raise SystemExit("智能中枢登录配置缺失或 OAuth 回调不匹配，停止启动。")
    if not fact_database().is_file():
        raise SystemExit("缺少履约事实库，停止启动。请按现有授权的数据流程准备站点数据。")
    init_database()
    log(f"启动 HTTPS 服务，端口 {os.environ['PORT']}")
    reexec(args)


if __name__ == "__main__":
    main()
