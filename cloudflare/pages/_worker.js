const HEALTH = "/__edge/health";
const PROXY_HEADER = "x-fulfillment-edge-key";
const PUBLIC_PATHS = new Set(["/", "/login", "/sharing", "/assistant.html", "/favicon.ico", "/ops/auth/login", "/ops/auth/callback"]);
const METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]);
const APP_ASSETS = new Set(["/static/app.css", "/static/app.js", "/static/echarts.min.js", "/static/sharing.js"]);
const VISITOR = { username: "preview", display_name: "只读预览", role: "viewer",
  role_label: "访客", team: null, can_edit: false, preview: true, cloudSharing: false, opsAssistant: false };
const FORWARDED_HEADERS = ["accept", "content-type", "cookie", "origin", "sec-fetch-site",
  "x-csrf-token", "sign", "if-modified-since", "if-none-match", "user-agent"];

function httpsOrigin(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && url.pathname === "/" && !url.username &&
        !url.password && !url.search && !url.hash) return url.origin;
  } catch {
    // An unset or invalid origin leaves the edge closed.
  }
  return null;
}

function privateHeaders(headers) {
  const result = new Headers(headers);
  result.set("Cache-Control", "no-store");
  result.set("X-Content-Type-Options", "nosniff");
  result.set("Referrer-Policy", "no-referrer");
  result.set("X-Robots-Tag", "noindex");
  return result;
}

function unavailable(code, message, status = 503) {
  return Response.json({ error: code, message }, {status, headers: privateHeaders()});
}

function knownPath(path) {
  return PUBLIC_PATHS.has(path) || path.startsWith("/static/") ||
    (path.startsWith("/api/") && !path.startsWith("/api/docs") && !path.startsWith("/api/openapi"));
}

function redirectLocation(location, upstream, publicOrigin, path, configuredAuthorize) {
  try {
    const destination = new URL(location, upstream);
    if (destination.origin === upstream.origin || destination.origin === publicOrigin) {
      if (destination.pathname.startsWith("//")) return null;
      return destination.pathname + destination.search + destination.hash;
    }
    if (path === "/ops/auth/login" && configuredAuthorize) {
      const allowed = new URL(configuredAuthorize);
      if (allowed.protocol === "https:" && !destination.username && !destination.password &&
          destination.origin === allowed.origin && destination.pathname === allowed.pathname) return destination.href;
    }
  } catch {
    return null;
  }
  return null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const vpc = env.FULFILLMENT_PRIVATE_WORKER;
    const origin = vpc ? "http://localhost:8770" : httpsOrigin(env.FULFILLMENT_ORIGIN_URL);
    const key = env.FULFILLMENT_EDGE_KEY;
    const configured = Boolean(origin && origin !== url.origin &&
      typeof key === "string" && /^[A-Za-z0-9_-]{43,128}$/.test(key));
    if (url.pathname === HEALTH && request.method === "GET") {
      return Response.json({ status: "edge_ready", originConfigured: configured },
        { headers: privateHeaders() });
    }
    if (!METHODS.has(request.method)) return unavailable("method", "请求方法不受支持。", 405);
    if (!knownPath(url.pathname)) return unavailable("not_found", "没有找到这个页面。", 404);
    const visitor = env.FULFILLMENT_EDGE_PREVIEW === "true" && !/(?:^|;\s*)fd_session=/.test(request.headers.get("cookie") || "");
    if (env.ASSETS && ["GET", "HEAD"].includes(request.method) &&
        (APP_ASSETS.has(url.pathname) || (visitor && url.pathname === "/"))) {
      const asset = await env.ASSETS.fetch(request);
      if (asset.ok || asset.status === 304) {
        const headers = privateHeaders(asset.headers);
        if (APP_ASSETS.has(url.pathname)) headers.set("Cache-Control", "public, max-age=300");
        return new Response(asset.body, {status: asset.status, headers});
      }
    }
    if (visitor && request.method === "GET" && url.pathname === "/api/me") {
      return Response.json(VISITOR, {headers: privateHeaders()});
    }
    if (!configured) return unavailable("origin_unconfigured", "Cloudflare 入口已发布，履约数据服务尚未连接。");

    const upstream = new URL(origin);
    upstream.pathname = url.pathname;
    upstream.search = url.search;
    const headers = new Headers();
    for (const name of FORWARDED_HEADERS) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    headers.set(PROXY_HEADER, key);

    let response;
    try {
      const options = {
        method: request.method, headers, body: request.body, redirect: "manual", duplex: "half",
      };
      response = vpc ? await vpc.fetch(upstream.href, options) : await fetch(upstream, options);
    } catch {
      return unavailable("origin_unreachable", "履约数据服务暂时无法连接。", 502);
    }
    const responseHeaders = privateHeaders(response.headers);
    if (responseHeaders.has("location")) {
      const location = redirectLocation(responseHeaders.get("location"), upstream, url.origin,
        url.pathname, env.OPS_AUTHORIZE_URL);
      if (!location) return unavailable("origin_redirect", "履约数据服务返回了未配置的登录地址。", 502);
      responseHeaders.set("location", location);
    }
    return new Response(response.body, {
      status: response.status, statusText: response.statusText, headers: responseHeaders,
    });
  },
};
