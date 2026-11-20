"""Server-side settings and bounded requests to the configured OPS services."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GatewayError(Exception):
    def __init__(self, code, message, status=502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def setting(name):
    return os.environ.get(name, "").strip()


def https_url(name):
    value = setting(name)
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and not (url.username or url.password or url.fragment):
            return value
    except ValueError:
        pass
    return ""


def site_origin():
    value = https_url("OPS_SITE_ORIGIN")
    return value.rstrip("/") if value and urlsplit(value).path in ("", "/") and not urlsplit(value).query else ""


def required_url(name):
    value = https_url(name)
    if not value:
        raise GatewayError("UNCONFIGURED", "智能中枢能力尚未完成配置。", 503)
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = build_opener(NoRedirect())


def upstream(url, *, key="", method="GET", body=None, headers=None, timeout=15):
    options = {"Accept": "application/json", **(headers or {})}
    if key:
        options["Authorization"] = "Bearer " + key
    try:
        return opener.open(Request(url, data=body, headers=options, method=method), timeout=timeout)
    except HTTPError as error:
        return error
    except (URLError, OSError, ValueError) as error:
        raise GatewayError("UPSTREAM_UNREACHABLE", "无法连接智能中枢，请稍后重试。") from error


def bounded_read(response, maximum):
    try:
        raw = response.read(maximum + 1)
    except (OSError, ValueError) as error:
        raise GatewayError("UPSTREAM_UNREACHABLE", "读取智能中枢响应失败。") from error
    if len(raw) > maximum:
        raise GatewayError("RESPONSE_TOO_LARGE", "智能中枢响应超过本站处理上限。")
    return raw


def decode_json(raw):
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise GatewayError("BAD_RESPONSE", "智能中枢返回了无法识别的数据。") from error


def json_request(url, *, key="", method="GET", payload=None, form=None, timeout=15):
    headers = {}
    body = form
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode()
    with upstream(url, key=key, method=method, body=body, headers=headers, timeout=timeout) as response:
        raw = bounded_read(response, 2 * 1024 * 1024)
        return response.status, decode_json(raw) if raw and response.status < 400 else raw
