"""Authorization-code login into the existing fulfillment user/session model."""
import argparse
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import core
from .ops_gateway import GatewayError, https_url, json_request, setting, site_origin

router = APIRouter()
CALLBACK_PATH = "/ops/auth/callback"
BROWSER_COOKIE = "__Host-fd_ops_browser"
STATE_TTL = 300
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
ERRORS = {
    "unavailable": "智能中枢登录尚未配置，请联系管理员。",
    "state": "登录请求已失效，请重新发起登录。",
    "denied": "中枢授权未完成，请重新登录。",
    "exchange": "中枢登录验证失败，请重新登录或联系管理员。",
    "access": "该中枢账号暂未获得本站权限，请联系管理员开通。",
}


def enabled():
    callback = https_url("OPS_REDIRECT_URI")
    parts = urlsplit(callback)
    return bool(site_origin() and callback == site_origin() + CALLBACK_PATH and not parts.query
                and setting("OPS_CLIENT_ID") and setting("OPS_CLIENT_SECRET")
                and all(https_url(key) for key in ("OPS_AUTHORIZE_URL", "OPS_TOKEN_URL", "OPS_USERINFO_URL")))


def local_login_enabled():
    value = setting("OPS_LOCAL_LOGIN").lower()
    return value in ("1", "true") if value else not bool(setting("OPS_CLIENT_ID"))


def next_path(value):
    return value if value in ("/", "/assistant.html") else "/"


def redirect(path):
    return RedirectResponse(path, status_code=303, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    })


def failed(error, target="/"):
    return redirect("/login?" + urlencode({"error": error, "next": target}))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def init_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ops_oauth_state (
        state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, expires INTEGER NOT NULL, target TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS ops_oauth_identity (
        provider TEXT NOT NULL, subject TEXT NOT NULL, username TEXT NOT NULL REFERENCES users(username),
        PRIMARY KEY (provider, subject), UNIQUE (provider, username)
    )""")


@router.get("/ops/auth/login")
def begin(request: Request):
    target = next_path(request.query_params.get("next"))
    if not enabled():
        return failed("unavailable", target)
    state = secrets.token_urlsafe(32)
    browser = request.cookies.get(BROWSER_COOKIE, "")
    if not TOKEN_FORMAT.fullmatch(browser):
        browser = secrets.token_urlsafe(32)
    now = int(time.time())
    with core.ops() as db:
        init_tables(db)
        db.execute("DELETE FROM ops_oauth_state WHERE expires<=?", (now,))
        db.execute("INSERT INTO ops_oauth_state VALUES (?,?,?,?)",
                   (digest(state), digest(browser), now + STATE_TTL, target))
    auth = urlsplit(https_url("OPS_AUTHORIZE_URL"))
    parameters = dict(parse_qsl(auth.query))
    parameters.update(response_type="code", client_id=setting("OPS_CLIENT_ID"),
                      redirect_uri=setting("OPS_REDIRECT_URI"), scope="openid profile", state=state)
    response = redirect(urlunsplit(auth._replace(query=urlencode(parameters))))
    response.set_cookie(BROWSER_COOKIE, browser, httponly=True, secure=True,
                        samesite="lax", path="/", max_age=STATE_TTL)
    return response


def exchange(code):
    form = urlencode(dict(grant_type="authorization_code", client_id=setting("OPS_CLIENT_ID"),
                          client_secret=setting("OPS_CLIENT_SECRET"), code=code,
                          redirect_uri=setting("OPS_REDIRECT_URI"))).encode()
    status, token = json_request(https_url("OPS_TOKEN_URL"), method="POST", form=form)
    token = token.get("data", token) if isinstance(token, dict) else {}
    if status != 200 or not isinstance(token, dict):
        raise ValueError("authorization failed")
    access = token.get("access_token")
    expiry = int(token.get("expires_in") or 0)
    if expiry <= 0 or not isinstance(access, str) or not access or not access.isascii() or any(c.isspace() for c in access):
        raise ValueError("invalid access token")
    started = time.monotonic()
    status, profile = json_request(https_url("OPS_USERINFO_URL"), key=access)
    profile = profile.get("data", profile) if isinstance(profile, dict) else {}
    if status != 200 or not isinstance(profile, dict):
        raise ValueError("identity failed")
    subject = profile.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 512:
        raise ValueError("missing subject")
    remaining = expiry - int(time.monotonic() - started)
    if remaining <= 0:
        raise ValueError("expired access token")
    return profile, remaining


def provider_key():
    return https_url("OPS_USERINFO_URL") + "|" + setting("OPS_CLIENT_ID")


def resolve_user(profile):
    provider, subject = provider_key(), profile["sub"]
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        init_tables(db)
        mapped = db.execute("""SELECT u.username,u.active FROM ops_oauth_identity i
            JOIN users u ON u.username=i.username WHERE i.provider=? AND i.subject=?""",
                            (provider, subject)).fetchone()
        if mapped:
            return mapped["username"] if mapped["active"] else None
        if setting("OPS_ALLOW_VIEWER_SIGNUP").lower() not in ("1", "true"):
            return None
        username = "ops:" + digest(provider + "\0" + subject)
        display = next((name.strip()[:120] for name in (profile.get("name"), profile.get("preferred_username"))
                        if isinstance(name, str) and name.strip()), "中枢用户")
        db.execute("""INSERT INTO users(username,password_hash,display_name,role,created_at)
                      VALUES (?,?,?,'viewer',?)""", (username, "!ops-sso", display, core.now_iso()))
        db.execute("INSERT INTO ops_oauth_identity VALUES (?,?,?)", (provider, subject, username))
        return username


@router.get(CALLBACK_PATH)
def callback(request: Request):
    if not enabled():
        return failed("unavailable")
    state = request.query_params.get("state", "")
    browser = request.cookies.get(BROWSER_COOKIE, "")
    if len(request.query_params.getlist("state")) != 1 or not all(TOKEN_FORMAT.fullmatch(value) for value in (state, browser)):
        return failed("state")
    with core.ops() as db:
        init_tables(db)
        attempt = db.execute("""DELETE FROM ops_oauth_state
            WHERE state_hash=? AND browser_hash=? AND expires>? RETURNING target""",
                             (digest(state), digest(browser), int(time.time()))).fetchone()
    if not attempt:
        return failed("state")
    target = next_path(attempt["target"])
    if "error" in request.query_params:
        return failed("denied", target)
    code = request.query_params.get("code", "")
    if len(request.query_params.getlist("code")) != 1 or not code or len(code) > 4096:
        return failed("exchange", target)
    try:
        profile, ttl = exchange(code)
    except (GatewayError, ValueError, TypeError, OverflowError):
        return failed("exchange", target)
    username = resolve_user(profile)
    if not username:
        return failed("access", target)
    max_age = min(ttl, core.session_seconds())
    sid, _ = core.create_session(username, ip=request.client.host if request.client else "",
                                 ua=request.headers.get("user-agent", ""), max_age=max_age)
    core.drop_session(request.cookies.get(core.SESSION_COOKIE, ""))
    core.audit(username, "ops_login")
    response = redirect(target)
    core.set_session_cookie(request, response, sid, max_age)
    return response


def require_same_origin(request):
    origin = site_origin() or f"{request.url.scheme}://{request.url.netloc}"
    presented = request.headers.get("origin")
    if (presented and presented != origin) or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "请求来源校验失败")


def require_csrf(request):
    require_same_origin(request)
    sid = request.cookies.get(core.SESSION_COOKIE)
    browser_request = request.headers.get("origin") or request.headers.get("sec-fetch-site")
    if sid and (setting("OPS_CLIENT_ID") or browser_request):
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied.isascii() or not secrets.compare_digest(supplied, core.csrf_token(sid)):
            raise HTTPException(403, "安全校验失败，请刷新页面后重试")


def main():
    parser = argparse.ArgumentParser(description="将已核对的中枢 sub 关联到现有站点账号")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--local-user", required=True)
    args = parser.parse_args()
    if not setting("OPS_CLIENT_ID") or not https_url("OPS_USERINFO_URL"):
        parser.error("请先载入正式的中枢站点环境变量")
    if not args.subject.strip() or len(args.subject) > 512:
        parser.error("中枢 sub 格式无效")
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        init_tables(db)
        if not db.execute("SELECT 1 FROM users WHERE username=? AND active=1", (args.local_user,)).fetchone():
            parser.error("站内账号不存在或已停用")
        current = db.execute("""SELECT username FROM ops_oauth_identity
            WHERE provider=? AND (subject=? OR username=?)""",
                             (provider_key(), args.subject, args.local_user)).fetchall()
        if current:
            same = db.execute("""SELECT 1 FROM ops_oauth_identity
                WHERE provider=? AND subject=? AND username=?""",
                              (provider_key(), args.subject, args.local_user)).fetchone()
            if not same:
                parser.error("该身份或站内账号已关联，请核对现有绑定")
        else:
            db.execute("INSERT INTO ops_oauth_identity VALUES (?,?,?)", (provider_key(), args.subject, args.local_user))
    print("站内账号关联已保存")


if __name__ == "__main__":
    main()
