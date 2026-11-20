"""Run the fulfillment service behind an authenticated Cloudflare origin."""
import hashlib
import hmac
import re

from fastapi.responses import JSONResponse

from . import cloud_login, core
from .main import app
from .ops_auth import enabled, local_login_enabled
from .ops_gateway import setting, site_origin

HEADER = b"x-fulfillment-edge-key"


def configured_key():
    key = setting("FULFILLMENT_EDGE_KEY")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", key) or not site_origin():
        raise RuntimeError("必须配置线上 HTTPS 站点地址和 Cloudflare 来源密钥")
    return key.encode()


def check_operating_mode():
    configured_key()
    if not core.DUCK_DB.is_file() or not core.OPS_DB.is_file():
        raise RuntimeError("履约主库或操作库不存在")
    if core.open_test_enabled():
        return
    if not local_login_enabled():
        if enabled():
            return
        if cloud_login.enabled() and core.allowed_online_account(cloud_login.USERNAME):
            with core.ops() as db:
                if db.execute("""SELECT 1 FROM users WHERE username=? AND active=1 AND password_hash=?""",
                              (cloud_login.USERNAME, cloud_login.LOCAL_ACCOUNT_MARKER)).fetchone():
                    return
        raise RuntimeError("必须完成线上登录配置")

    from init_ops import SEED_USERS, verify_pw

    with core.ops() as db:
        accounts = {name: password for name, password in db.execute(
            "SELECT username,password_hash FROM users WHERE active=1") if core.allowed_online_account(name)}
    if not accounts:
        raise RuntimeError("必须配置正式站内账号")
    for username, password, *_ in SEED_USERS:
        if username in accounts and verify_pw(password, accounts[username]):
            raise RuntimeError("仍有启用的示例账号使用初始密码")


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        check_operating_mode()
    elif scope["type"] == "http":
        keys = [value for name, value in scope["headers"] if name == HEADER]
        supplied = keys[0] if len(keys) == 1 else b""
        expected = configured_key()
        if not hmac.compare_digest(hashlib.sha256(supplied).digest(), hashlib.sha256(expected).digest()):
            return await JSONResponse({"detail": "需要通过履约监控站点访问"}, status_code=403)(scope, receive, send)
    await app(scope, receive, send)
