"""Exercise the FastAPI OPS integration without network access or production data."""
import asyncio
from contextlib import closing
from email.message import Message
from hashlib import sha256
from http.cookies import SimpleCookie
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from app import cloud_login, core, ops_auth
from app.main import app
from init_ops import SCHEMA, hash_pw

SITE = "https://site.example"
HUB = "https://hub.example"
SKILL = b"# Warehouse\nUse warehouse processing data."
ENV = {
    "OPS_SITE_ORIGIN": SITE, "OPS_CLIENT_ID": "mock-client", "OPS_CLIENT_SECRET": "mock-oauth-key",
    "OPS_REDIRECT_URI": SITE + "/ops/auth/callback", "OPS_AUTHORIZE_URL": HUB + "/authorize",
    "OPS_TOKEN_URL": HUB + "/token", "OPS_USERINFO_URL": HUB + "/userinfo",
    "OPS_LOCAL_LOGIN": "false", "OPS_ALLOW_VIEWER_SIGNUP": "false",
    "OPS_SITE_MCP_URL": HUB + "/mcp", "OPS_SITE_MCP_KEY": "mock-mcp-key",
    "OPS_MCP_ALLOWED_TOOLS": "warehouse_summary",
    "OPS_SITE_AI_URL": HUB + "/ai", "OPS_SITE_AI_KEY": "mock-ai-key", "OPS_AI_PROTOCOL": "",
    "OPS_ASSISTANT_ROLES": "admin", "OPS_SITE_SKILLS_URL": HUB + "/skills",
    "FULFILLMENT_PUBLIC_PREVIEW": "false", "FULFILLMENT_OPEN_TEST": "false",
}


class Response(BytesIO):
    def __init__(self, payload=b"", status=200, content_type="application/json"):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class Browser:
    def __init__(self):
        self.cookies = SimpleCookie()

    def request(self, url, method="GET", data=None, headers=None):
        parsed = urlsplit(url)
        body = json.dumps(data).encode() if data is not None else b""
        headers = {
            "host": "site.example", "content-type": "application/json",
            "cookie": "; ".join(f"{key}={item.value}" for key, item in self.cookies.items()),
            **(headers or {}),
        }
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "https", "path": parsed.path, "root_path": "",
                 "query_string": parsed.query.encode(), "client": ("127.0.0.1", 12345),
                 "server": ("site.example", 443), "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        events = []

        async def receive():
            return {"type": "http.request", "body": body}

        async def send(message):
            events.append(message)

        asyncio.run(app(scope, receive, send))
        head = events[0]
        for key, value in head["headers"]:
            if key == b"set-cookie":
                self.cookies.load(value.decode())
        return head["status"], dict(head["headers"]), b"".join(e.get("body", b"") for e in events[1:])


class OpsTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        db = Path(temp) / "ops.db"
        self.enterContext(patch.object(core, "OPS_DB", db))
        self.enterContext(patch.dict("os.environ", ENV))
        self.enterContext(patch("app.ops_gateway.opener", self))
        self.browser = Browser()
        self.token_calls = 0
        self.usage = []
        self.bad_hash = False
        self.quota = False
        self.mcp_status = 200
        with closing(sqlite3.connect(db)) as con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)",
                        ("operator", hash_pw("test-password"), "admin", core.now_iso()))
            ops_auth.init_tables(con)
            con.execute("INSERT INTO ops_oauth_identity VALUES (?,?,?)",
                        (ops_auth.provider_key(), "stable-subject", "operator"))
            con.commit()

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        self.assertEqual(url.netloc, "hub.example")
        authorization = request.get_header("Authorization")
        if url.path == "/token":
            self.token_calls += 1
            self.assertEqual(parse_qs(request.data.decode()), {
                "grant_type": ["authorization_code"], "client_id": ["mock-client"],
                "client_secret": ["mock-oauth-key"], "code": ["mock-code"],
                "redirect_uri": [ENV["OPS_REDIRECT_URI"]],
            })
            return Response({"access_token": "mock-access", "expires_in": 120})
        if url.path == "/userinfo":
            self.assertEqual(authorization, "Bearer mock-access")
            return Response({"sub": "stable-subject", "preferred_username": "display-only"})
        if url.path == "/mcp":
            self.assertEqual(authorization, "Bearer mock-mcp-key")
            if self.mcp_status != 200:
                return Response(status=self.mcp_status)
            if request.get_method() == "DELETE":
                return Response(status=204)
            self.assertEqual(request.get_header("Accept"), "application/json, text/event-stream")
            message = json.loads(request.data)
            method = message["method"]
            if method != "initialize":
                self.assertEqual(request.get_header("Mcp-session-id"), "test-session")
                self.assertEqual(request.get_header("Mcp-protocol-version"), "2025-06-18")
            if method == "notifications/initialized":
                return Response(status=202)
            results = {
                "initialize": {"protocolVersion": "2025-06-18"},
                "tools/list": {"tools": [{"name": "warehouse_summary"}, {"name": "not_allowed"}]},
                "tools/call": {"content": [{"text": "matched"}]},
            }
            payload = {"jsonrpc": "2.0", "id": message["id"], "result": results[method]}
            if method == "tools/call":
                self.assertEqual(message["params"]["name"], "warehouse_summary")
                return Response(b"data: " + json.dumps(payload).encode() + b"\n\n", content_type="text/event-stream")
            response = Response(payload)
            if method == "initialize":
                response.headers["Mcp-Session-Id"] = "test-session"
            return response
        self.assertEqual(authorization, "Bearer mock-ai-key")
        if url.path == "/ai/models":
            return Response({"data": [{"id": "chat"}, {"id": "claude"},
                                      {"id": "response", "protocol": "responses"}]})
        if url.path.startswith("/ai/"):
            if self.quota:
                return Response({"error": "quota"}, 402)
            if url.path == "/ai/responses":
                self.assertIn(SKILL.decode(), json.loads(request.data)["instructions"])
                return Response({"output_text": "response reply"})
            if url.path == "/ai/messages":
                return Response({"content": [{"type": "text", "text": "messages reply"}]})
            self.assertEqual(url.path, "/ai/chat/completions")
            return Response({"choices": [{"message": {"content": "chat reply"}}]})
        if url.path == "/skills":
            checksum = "0" * 64 if self.bad_hash else sha256(SKILL).hexdigest()
            return Response({"skills": [{"id": "sample", "downloadPath": "/package", "sha256": checksum}]})
        if url.path == "/package":
            return Response(SKILL)
        self.assertEqual(url.path, "/skills/usage")
        self.usage.append(json.loads(request.data))
        return Response({"ok": True})

    def begin(self, target="/"):
        status, headers, _ = self.browser.request("/ops/auth/login?" + urlencode({"next": target}))
        self.assertEqual(status, 303)
        params = parse_qs(urlsplit(headers[b"location"].decode()).query)
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["scope"], ["openid profile"])
        self.assertEqual(params["redirect_uri"], [ENV["OPS_REDIRECT_URI"]])
        self.assertIn(b"Secure", headers[b"set-cookie"])
        return "/ops/auth/callback?" + urlencode({"state": params["state"][0], "code": "mock-code"})

    def login(self):
        status, headers, _ = self.browser.request(self.begin("/assistant.html"))
        self.assertEqual((status, headers[b"location"]), (303, b"/assistant.html"))
        for item in (b"HttpOnly", b"Secure", b"SameSite=lax", b"Max-Age=120"):
            self.assertIn(item, headers[b"set-cookie"])
        status, _, body = self.browser.request("/api/me")
        self.assertEqual(status, 200)
        profile = json.loads(body)
        self.assertEqual(profile["username"], "operator")
        return {"x-csrf-token": profile["csrf"], "origin": SITE}

    def test_state_and_session(self):
        callback = self.begin("//outside.example")
        self.assertIn(b"error=state", Browser().request(callback)[1][b"location"])
        self.assertEqual(self.token_calls, 0)
        self.assertEqual(self.browser.request(callback)[1][b"location"], b"/")
        self.assertIn(b"error=state", self.browser.request(callback)[1][b"location"])
        callback = self.begin()
        with core.ops() as db:
            db.execute("UPDATE ops_oauth_state SET expires=0")
        self.assertIn(b"error=state", self.browser.request(callback)[1][b"location"])
        self.assertIn(b"error=denied", self.browser.request(self.begin() + "&error=access_denied")[1][b"location"])
        self.assertEqual(self.token_calls, 1)
        csrf = self.login()
        self.assertEqual(self.browser.request("/api/logout", "POST")[0], 403)
        self.assertEqual(self.browser.request("/api/logout", "POST", headers={**csrf, "origin": HUB})[0], 403)
        self.assertEqual(self.browser.request("/api/logout", "POST", headers=csrf)[0], 200)
        self.assertEqual(self.browser.request("/api/me")[0], 401)

    def test_account_modes(self):
        with core.ops() as db:
            db.execute("DELETE FROM ops_oauth_identity")
        self.assertIn(b"error=access", self.browser.request(self.begin())[1][b"location"])
        credentials = {"username": "operator", "password": "test-password"}
        self.assertEqual(self.browser.request("/api/login", "POST", credentials)[0], 403)
        with patch.dict("os.environ", {"OPS_LOCAL_LOGIN": "true"}):
            self.assertEqual(self.browser.request("/api/login", "POST", credentials)[0], 200)

    def test_cloud_ticket_is_local_scope_single_use_and_expires(self):
        from cloudflare.open_online import prepare_account

        with patch.dict("os.environ", {"FULFILLMENT_CLOUD_TICKETS": "true",
                                      "FULFILLMENT_ONLINE_USERS": cloud_login.USERNAME}):
            prepare_account()
            body = {"ticket": cloud_login.issue_ticket()}
            path = "/api/cloud/session"
            self.assertEqual(self.browser.request(path, "POST", body, {"origin": HUB})[0], 403)
            self.assertEqual(self.browser.request(path, "POST", body, {"origin": SITE})[0], 200)
            self.assertEqual(self.browser.request(path, "POST", body, {"origin": SITE})[0], 401)
            profile = json.loads(self.browser.request("/api/me")[2])
            self.assertEqual(profile["username"], cloud_login.USERNAME)
            expired = {"ticket": cloud_login.issue_ticket()}
            with core.ops() as db:
                db.execute("UPDATE cloud_login_ticket SET expires=0")
            self.assertEqual(self.browser.request(path, "POST", expired, {"origin": SITE})[0], 401)

    def test_cloud_invites_create_viewers_and_revoke_all_viewer_sessions(self):
        from cloudflare.open_online import prepare_account

        manage = "/api/cloud/invites"
        accept = "/api/cloud/invite/session"
        with patch.dict("os.environ", {"FULFILLMENT_CLOUD_TICKETS": "true",
                                      "FULFILLMENT_ONLINE_USERS": cloud_login.USERNAME}):
            prepare_account()
            origin = {"origin": SITE}
            self.assertEqual(Browser().request(manage, "POST", {"name": "Demo"}, origin)[0], 401)
            self.assertEqual(self.browser.request("/api/cloud/session", "POST",
                                                   {"ticket": cloud_login.issue_ticket()}, origin)[0], 200)
            _, _, identity = self.browser.request("/api/me")
            admin = {**origin, "x-csrf-token": json.loads(identity)["csrf"]}
            self.assertEqual(self.browser.request(manage, "POST", {"name": "Demo"}, origin)[0], 403)
            status, _, new_invite = self.browser.request(manage, "POST", {"name": "Demo"}, admin)
            self.assertEqual(status, 201)
            link = urlsplit(json.loads(new_invite)["link"])
            self.assertEqual(link.path, "/login")
            ticket = {"ticket": parse_qs(link.fragment)["invite"][0]}
            viewer = Browser()
            self.assertEqual(viewer.request(accept, "POST", ticket, {"origin": HUB})[0], 403)
            status, headers, _ = viewer.request(accept, "POST", ticket, origin)
            self.assertEqual(status, 200)
            self.assertIn(b"Max-Age=604800", headers[b"set-cookie"])
            self.assertIn(b"Secure", headers[b"set-cookie"])
            self.assertEqual(Browser().request(accept, "POST", ticket, origin)[0], 401)
            profile = json.loads(viewer.request("/api/me")[2])
            self.assertEqual(profile["role"], "viewer")
            self.assertFalse(profile["can_edit"])
            self.assertFalse(profile["cloudSharing"])
            self.assertEqual(viewer.request(manage)[0], 403)
            with patch.dict("os.environ", {"FULFILLMENT_PUBLIC_PREVIEW": "true"}):
                profile_after_toggle = json.loads(viewer.request("/api/me")[2])
                self.assertTrue(profile_after_toggle["preview"])
                self.assertEqual(self.browser.request(manage)[0], 404)
                self.assertEqual(viewer.request(accept, "POST", ticket, origin)[0], 404)
                self.assertEqual(viewer.request("/api/orders")[0], 401)
            path = manage + "/" + profile["username"]
            replacement = self.browser.request(path + "/renew", "POST", headers=admin)
            self.assertEqual(replacement[0], 200)
            self.assertEqual(self.browser.request(path, "DELETE", headers=admin)[0], 200)
            self.assertEqual(viewer.request("/api/me")[0], 401)
            renewed = {"ticket": parse_qs(urlsplit(json.loads(replacement[2])["link"]).fragment)["invite"][0]}
            self.assertEqual(Browser().request(accept, "POST", renewed, origin)[0], 401)

    def test_public_preview_without_cookie_preserves_management_permissions(self):
        with patch.dict("os.environ", {"FULFILLMENT_PUBLIC_PREVIEW": "true"}):
            visitor = Browser()
            self.assertEqual(visitor.request("/")[0], 200)
            status, headers, body = visitor.request("/api/me")
            self.assertEqual(status, 200)
            self.assertNotIn(b"set-cookie", headers)
            identity = json.loads(body)
            self.assertTrue(identity["preview"])
            self.assertFalse(identity["can_edit"])
            self.assertNotIn("csrf", identity)
            for query in ("", "?q=%25", "?q=%23"):
                self.assertEqual(json.loads(visitor.request("/api/track" + query)[2])["rows"], [])
            self.assertEqual(visitor.request("/api/orders/TEST123")[0], 404)
            for path in ("/api/orders", "/api/alerts", "/api/health", "/api/rules"):
                self.assertEqual(visitor.request(path)[0], 401)
            self.assertEqual(visitor.request("/sharing")[0], 404)
            self.assertEqual(visitor.request("/api/orders/TEST123/notes", "POST",
                                             {"body": "blocked"}, {"origin": SITE})[0], 401)
            self.login()
            admin = json.loads(self.browser.request("/api/me")[2])
            self.assertTrue(admin["can_edit"])
            self.assertFalse(admin["preview"])
            self.assertFalse(admin["cloudSharing"])

    def test_open_test_has_full_access_without_login_or_cookies(self):
        with patch.dict("os.environ", {"FULFILLMENT_OPEN_TEST": "true", "FULFILLMENT_CLOUD_TICKETS": "true"}):
            visitor = Browser()
            self.assertEqual(visitor.request("/")[0], 200)
            for path in ("/login", "/ops/auth/login"):
                status, headers, _ = visitor.request(path)
                self.assertEqual((status, headers[b"location"]), (303, b"/"))
                self.assertNotIn(b"set-cookie", headers)
            status, headers, result = visitor.request("/api/me")
            self.assertEqual(status, 200)
            self.assertNotIn(b"set-cookie", headers)
            identity = json.loads(result)
            self.assertTrue(identity["openTest"])
            self.assertTrue(identity["can_edit"])
            self.assertFalse(identity["preview"])
            self.assertNotIn("csrf", identity)
            self.assertEqual(visitor.request("/api/cloud/session", "POST", {"ticket": "A" * 43})[0], 404)
            self.assertEqual(visitor.request("/sharing")[0], 404)
            with core.ops() as db:
                db.execute("INSERT INTO alerts(order_no,rule) VALUES ('TEST123','sample')")
            change = {"order_no": "TEST123", "rule": "sample", "action": "resolve"}
            path = "/api/alerts/action"
            self.assertEqual(visitor.request(path, "POST", change, {"origin": HUB})[0], 403)
            status, _, result = visitor.request(path, "POST", change, {"origin": SITE, "cookie": "fd_session=old"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(result)["status"], "resolved")
            with core.ops() as db:
                action = db.execute("SELECT status, updated_by FROM alert_state").fetchone()
                self.assertEqual(tuple(action), ("resolved", "open-test"))

    def test_alerts_show_newest_dispatch_and_suspend_postnet(self):
        with patch.dict("os.environ", {"FULFILLMENT_OPEN_TEST": "true"}):
            with core.ops() as db:
                db.executemany(
                    "INSERT INTO alerts(order_no,rule,carrier,shipped_date,shipped_at) VALUES (?,'sample',?,?,?)",
                    [("A", "UPS", "2026-11-18", "2026-11-18 09:00:00"),
                     ("B", "FedEx", "2026-11-18", "2026-11-18 18:00:00"),
                     ("C", " POSTNET ", "2026-11-19", "2026-11-19 09:00:00"),
                     ("D", "Posnet", "2026-11-20", "2026-11-20 09:00:00"),
                     ("E", None, None, None)]
                )
            for page, order_no in enumerate(("B", "A", "E"), 1):
                status, _, result = self.browser.request(f"/api/alerts?page={page}&size=1")
                self.assertEqual(status, 200)
                data = json.loads(result)
                self.assertEqual(data["total"], 3)
                self.assertEqual(data["rows"][0]["order_no"], order_no)
                self.assertEqual(sum(row["n"] for row in data["summary"]), 3)
            status, _, result = self.browser.request("/api/alerts?cr=Posnet")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(result)["rows"], [])
            with core.ops() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM alerts").fetchone()[0], 5)

    def test_mcp(self):
        csrf = self.login()
        status, _, body = self.browser.request("/api/mcp/tools")
        self.assertEqual((status, json.loads(body)["tools"]), (200, [{"name": "warehouse_summary"}]))
        path = "/api/mcp/call"
        self.assertEqual(self.browser.request(path, "POST", {"name": "not_allowed"}, csrf)[0], 403)
        status, _, body = self.browser.request(path, "POST", {"name": "warehouse_summary"}, csrf)
        self.assertEqual((status, json.loads(body)["text"]), (200, "matched"))
        for status, code in ((401, "MCP_UNAUTHORIZED"), (403, "MCP_FORBIDDEN"), (404, "MCP_DISABLED")):
            self.mcp_status = status
            self.assertEqual(json.loads(self.browser.request("/api/mcp/tools")[2])["error"], code)

    def test_models_and_skill(self):
        csrf = self.login()
        self.assertEqual(len(json.loads(self.browser.request("/api/ai/models")[2])["models"]), 3)
        self.assertEqual(self.browser.request("/api/skills/sample/instructions")[0], 200)
        self.assertEqual(self.usage, [])
        message = {"messages": [{"role": "user", "content": "hello"}]}
        skill = {**message, "model": "response", "skill_id": "sample"}
        path = "/api/ai/chat"
        self.bad_hash = True
        result = self.browser.request(path, "POST", skill, csrf)
        self.assertEqual((result[0], json.loads(result[2])["error"]), (502, "SKILL_HASH_MISMATCH"))
        self.bad_hash, self.quota = False, True
        result = self.browser.request(path, "POST", skill, csrf)
        self.assertEqual((result[0], json.loads(result[2])["error"]), (402, "AI_QUOTA"))
        self.assertFalse(self.usage)
        self.quota = False
        for model, reply in (("chat", "chat reply"), ("claude", "messages reply")):
            result = self.browser.request(path, "POST", {**message, "model": model}, csrf)
            self.assertEqual((result[0], json.loads(result[2])["text"]), (200, reply))
        status, _, body = self.browser.request(path, "POST", skill, csrf)
        self.assertEqual((status, json.loads(body)["usageReported"]), (200, True))
        self.assertEqual(self.usage[0]["ids"], ["sample"])
        self.assertEqual(self.usage[0]["engine"], "logistics-dashboard")
        self.assertTrue(self.usage[0]["eventKey"])


if __name__ == "__main__":
    unittest.main()
