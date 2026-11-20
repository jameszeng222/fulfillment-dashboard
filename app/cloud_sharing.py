"""Manage independently revocable invitations to read cloud fulfillment data."""
import secrets
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import cloud_login, core
from .ops_auth import TOKEN_FORMAT, digest, require_csrf, require_same_origin
from .ops_gateway import site_origin

router = APIRouter(prefix="/api/cloud")
INVITE_TTL = 86400
VIEWER_SESSION_TTL = 7 * 86400


def require_sharing(request):
    if not cloud_login.enabled() or not site_origin():
        raise HTTPException(404, "邀请功能尚未启用")
    actor = core.require_user(request)
    if actor["role"] != "admin":
        raise HTTPException(403, "只有管理员可以管理访问")
    if request.method != "GET":
        require_csrf(request)
    return actor


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS cloud_invite (
        username TEXT PRIMARY KEY REFERENCES users(username),
        token_hash TEXT NOT NULL UNIQUE,
        invited_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        redeemed_at INTEGER,
        revoked_at INTEGER
    )""")


def invite_link(token, expires):
    return {"link": f"{site_origin()}/login#invite={token}", "expires": expires}


class InviteIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get("/invites")
def list_invites(request: Request, response: Response):
    require_sharing(request)
    now = int(time.time())
    with core.ops() as db:
        ensure_table(db)
        records = db.execute("""SELECT i.username, u.display_name AS name, i.expires_at AS expires,
                   CASE WHEN i.revoked_at IS NOT NULL THEN 'revoked'
                        WHEN EXISTS (SELECT 1 FROM sessions s
                                     WHERE s.username=i.username AND julianday(s.expires_at)>julianday('now'))
                             THEN 'active'
                        WHEN i.redeemed_at IS NULL AND i.expires_at>? THEN 'pending'
                        ELSE 'expired' END AS status
            FROM cloud_invite i JOIN users u ON u.username=i.username ORDER BY i.invited_at DESC, i.username""",
                             (now,)).fetchall()
    response.headers["Cache-Control"] = "no-store"
    return {"items": [dict(row) for row in records]}


@router.post("/invites", status_code=201)
def create_invite(body: InviteIn, request: Request, response: Response):
    actor = require_sharing(request)
    name = body.name.strip()
    if not name or not name.isprintable():
        raise HTTPException(422, "请输入同事的称呼")
    username = "cloud-viewer:" + uuid4().hex
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with core.ops() as db:
        ensure_table(db)
        db.execute("""INSERT INTO users (username,password_hash,display_name,role,active,created_at)
                      VALUES (?,?,?,'viewer',0,?)""",
                   (username, core.CLOUD_VIEWER_MARKER, name, core.now_iso()))
        db.execute("""INSERT INTO cloud_invite (username,token_hash,invited_at,expires_at) VALUES (?,?,?,?)""",
                   (username, digest(token), now, now + INVITE_TTL))
        core.audit(actor["username"], "cloud_invite_created", username, connection=db)
    response.headers["Cache-Control"] = "no-store"
    return invite_link(token, now + INVITE_TTL)


@router.post("/invites/{username}/renew")
def renew_invite(username: str, request: Request, response: Response):
    actor = require_sharing(request)
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with core.ops() as db:
        ensure_table(db)
        count = db.execute("""UPDATE cloud_invite SET token_hash=?,invited_at=?,expires_at=?,redeemed_at=NULL
                              WHERE username=? AND revoked_at IS NULL""",
                           (digest(token), now, now + INVITE_TTL, username)).rowcount
        if not count:
            raise HTTPException(404, "邀请不存在或已撤销")
        core.audit(actor["username"], "cloud_invite_renewed", username, connection=db)
    response.headers["Cache-Control"] = "no-store"
    return invite_link(token, now + INVITE_TTL)


@router.delete("/invites/{username}")
def revoke_invite(username: str, request: Request):
    actor = require_sharing(request)
    with core.ops() as db:
        ensure_table(db)
        updated = db.execute("UPDATE cloud_invite SET revoked_at=? WHERE username=? AND revoked_at IS NULL",
                             (int(time.time()), username)).rowcount
        if not updated:
            raise HTTPException(404, "邀请不存在或已撤销")
        db.execute("UPDATE users SET active=0 WHERE username=?", (username,))
        db.execute("DELETE FROM sessions WHERE username=?", (username,))
        core.audit(actor["username"], "cloud_invite_revoked", username, connection=db)
    return {"ok": True}


@router.post("/invite/session")
def redeem_invite(body: cloud_login.TicketIn, request: Request, response: Response):
    if not cloud_login.enabled() or not site_origin():
        raise HTTPException(404, "邀请功能尚未启用")
    require_same_origin(request)
    if not TOKEN_FORMAT.fullmatch(body.ticket):
        raise HTTPException(401, "邀请无效，请联系管理员生成新链接")
    now = int(time.time())
    with core.ops() as db:
        ensure_table(db)
        row = db.execute("""UPDATE cloud_invite SET redeemed_at=? WHERE token_hash=? AND expires_at>?
                   AND redeemed_at IS NULL AND revoked_at IS NULL RETURNING username""",
                         (now, digest(body.ticket), now)).fetchone()
        if not row:
            raise HTTPException(401, "邀请已使用、过期或撤销，请联系管理员生成新链接")
        username = row["username"]
        db.execute("UPDATE users SET active=1 WHERE username=? AND role='viewer' AND password_hash=?",
                   (username, core.CLOUD_VIEWER_MARKER))
        core.audit(username, "cloud_invite_accepted", connection=db)
    sid, _ = core.create_session(username, ip=request.client.host if request.client else "",
                                 ua=request.headers.get("user-agent", ""), max_age=VIEWER_SESSION_TTL)
    core.set_session_cookie(request, response, sid, VIEWER_SESSION_TTL)
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}
