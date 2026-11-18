"""应用入口：uvicorn app.main:app --reload"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import core
from .api import router

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "templates"
STATIC = ROOT / "static"

app = FastAPI(title="履约追踪系统", version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _page(name: str) -> FileResponse:
    return FileResponse(TPL / name, media_type="text/html; charset=utf-8")


@app.get("/login")
def login_page(request: Request):
    if core.current_user(request):
        return RedirectResponse("/", status_code=302)
    return _page("login.html")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    u = core.current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=302)
    return _page("app.html")




@app.get("/favicon.ico")
def favicon():
    return HTMLResponse(status_code=204)


@app.on_event("startup")
def _startup():
    if not core.OPS_DB.exists():
        print("[warn] ops.db 不存在，先运行: python init_ops.py")
    print(f"事实层: {core.DUCK_DB}")
    print(f"操作层: {core.OPS_DB}")
