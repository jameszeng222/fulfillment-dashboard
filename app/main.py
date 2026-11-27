"""应用入口：uvicorn app.main:app --reload"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from html import escape
import logging
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import cloud_login, cloud_sharing, core, ops_auth, ops_assistant, tracking_full
from .ops_gateway import GatewayError
from .api import router
from .pages_api import router as pages_router
from .tracking_sync import router as tracking_sync_router
from .trajectory_rules import router as trajectory_rules_router

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "templates"
STATIC = ROOT / "static"

@asynccontextmanager
async def lifespan(_app):
    _startup()
    worker = asyncio.create_task(tracking_full.dispatch())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(title="履约监控系统", version="0.2.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)


def protect_site_mutations(request: Request):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path != "/api/login":
        ops_auth.require_csrf(request)


app.include_router(router, dependencies=[Depends(protect_site_mutations)])
app.include_router(pages_router, dependencies=[Depends(protect_site_mutations)])
app.include_router(trajectory_rules_router, dependencies=[Depends(protect_site_mutations)])
app.include_router(tracking_sync_router)
app.include_router(ops_auth.router)
app.include_router(ops_assistant.router)
app.include_router(cloud_login.router)
app.include_router(cloud_sharing.router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.exception_handler(GatewayError)
def ops_error(request: Request, error: GatewayError):
    return ops_assistant.error_response(error)


def _page(name: str) -> FileResponse:
    return FileResponse(TPL / name, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/login")
def login_page(request: Request):
    target = ops_auth.next_path(request.query_params.get("next"))
    if core.open_test_enabled():
        return ops_auth.redirect(target)
    error = ops_auth.ERRORS.get(request.query_params.get("error"), "")
    if not error and core.current_user(request):
        return ops_auth.redirect(target)
    button = ""
    if ops_auth.enabled():
        link = "/ops/auth/login?" + urlencode({"next": target})
        button = f'<a class="btn" style="display:block;text-align:center" href="{escape(link, quote=True)}">使用智能中枢登录</a>'
    elif not ops_auth.local_login_enabled() and not cloud_login.enabled():
        error = error or ops_auth.ERRORS["unavailable"]
    if core.public_preview_enabled():
        access = '<p><a href="/">进入只读预览</a></p>'
    else:
        access = '<p>请打开管理员发给你的邀请链接。本机管理员可使用桌面免密码入口。</p>' if cloud_login.enabled() else ""
    html = (TPL / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<!--OPS_LOGIN-->", button)
                        .replace("<!--LOGIN_MESSAGE-->", escape(error))
                        .replace("<!--CLOUD_LOGIN-->", access)
                        .replace("<!--INVITE_STATE-->", "false" if core.public_preview_enabled() else "true")
                        .replace("<!--LOCAL_LOGIN_STATE-->", "" if ops_auth.local_login_enabled() else "hidden"),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not core.public_preview_enabled() and not core.current_user(request):
        return RedirectResponse("/login", status_code=302)
    return _page("app.html")


@app.get("/sharing")
def sharing_page(request: Request):
    if core.public_preview_enabled():
        raise HTTPException(404, "邀请功能尚未启用")
    if not core.current_user(request):
        return ops_auth.redirect("/login")
    cloud_sharing.require_sharing(request)
    return _page("sharing.html")


@app.get("/assistant.html")
def assistant_page(request: Request):
    user = core.current_user(request)
    if not user:
        return ops_auth.redirect("/login?next=%2Fassistant.html")
    if not ops_assistant.can_use(user):
        raise HTTPException(403, "当前角色没有使用智能助手的权限")
    html = (ROOT / "public" / "assistant.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<body>", '<body data-fulfillment-ops="true">', 1),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


class HideCallbackQuery(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if isinstance(path, str) and path.partition("?")[0] == ops_auth.CALLBACK_PATH:
                record.args = client, method, ops_auth.CALLBACK_PATH, version, status
        return True



@app.get("/favicon.ico")
def favicon():
    return HTMLResponse(status_code=204)


def _startup():
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, HideCallbackQuery) for item in access.filters):
        access.addFilter(HideCallbackQuery())
    if not core.OPS_DB.exists():
        print("[warn] ops.db 不存在，先运行: python init_ops.py")
    elif core.DUCK_DB.exists():
        from alerts import migrate_alert_history, migrate_alert_order_fields
        migrate_alert_order_fields()
        migrate_alert_history()
    print(f"事实层: {core.DUCK_DB}")
    print(f"操作层: {core.OPS_DB}")
