"""Exchange a one-use local Windows login link for a web session."""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import core
from .ops_auth import TOKEN_FORMAT, digest, require_same_origin
from .ops_gateway import setting

router = APIRouter()
USERNAME = "fulfillment-online"
LOCAL_ACCOUNT_MARKER = "!local-windows-ticket"
TTL = 90


def enabled():
    return not core.open_test_enabled() and setting("FULFILLMENT_CLOUD_TICKETS").lower() == "true"


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS cloud_login_ticket (
        token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL
    )""")


def issue_ticket():
    ticket = secrets.token_urlsafe(32)
    now = int(time.time())
    with core.ops() as db:
        ensure_table(db)
        db.execute("DELETE FROM cloud_login_ticket WHERE expires<=?", (now,))
        db.execute("INSERT INTO cloud_login_ticket VALUES (?,?)", (digest(ticket), now + TTL))
    return ticket


class TicketIn(BaseModel):
    ticket: str = Field(min_length=43, max_length=43)


@router.post("/api/cloud/session")
def login(body: TicketIn, request: Request, response: Response):
    if not enabled():
        raise HTTPException(404, "找不到该登录入口")
    require_same_origin(request)
    if not TOKEN_FORMAT.fullmatch(body.ticket) or not core.allowed_online_account(USERNAME):
        raise HTTPException(401, "登录链接无效或已过期")
    with core.ops() as db:
        ensure_table(db)
        ticket = db.execute("""DELETE FROM cloud_login_ticket WHERE token_hash=? AND expires>?
                               RETURNING token_hash""", (digest(body.ticket), int(time.time()))).fetchone()
        account = db.execute("SELECT active,password_hash FROM users WHERE username=?", (USERNAME,)).fetchone()
    if not ticket or not account or not account["active"] or account["password_hash"] != LOCAL_ACCOUNT_MARKER:
        raise HTTPException(401, "登录链接无效或已使用，请从本机重新打开")
    sid, _ = core.create_session(USERNAME, ip=request.client.host if request.client else "",
                                 ua=request.headers.get("user-agent", ""))
    core.drop_session(request.cookies.get(core.SESSION_COOKIE, ""))
    core.audit(USERNAME, "cloud_local_ticket_login")
    core.set_session_cookie(request, response, sid)
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}
