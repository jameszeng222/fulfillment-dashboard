/**
 * 智能中枢 OAuth（Worker 版）。
 *
 * 与 Node 版的差异：Workers 没有跨请求的进程内存，所以
 *  · 会话改成「签名 Cookie」—— HMAC-SHA256 保护，服务端无状态
 *  · state 也存在签名 Cookie 里，天然绑定当前浏览器、限时有效
 *  · 授权码本身一次性，回调重放会在换 token 那一步被中枢拒绝
 */

import {
  sign, unsign, parseCookies, serializeCookie, clearCookie,
  safeEqual, randomToken, timeoutFetch, describeUpstream, safeNext,
  page, errorPage, htmlResponse, redirectResponse, loginRequired, esc,
} from './util.mjs';

export const STATE_COOKIE = 'ops_state';
export const SID_COOKIE = 'ops_sid';

const REQUIRED = [
  'OPS_SITE_ORIGIN', 'OPS_CLIENT_ID', 'OPS_REDIRECT_URI', 'OPS_AUTHORIZE_URL',
  'OPS_TOKEN_URL', 'OPS_USERINFO_URL', 'SESSION_SECRET',
];

export function loadConfig(env) {
  const missing = REQUIRED.filter((k) => !env[k]);
  return {
    ok: missing.length === 0,
    missing,
    origin: (env.OPS_SITE_ORIGIN || '').replace(/\/+$/, ''),
    clientId: env.OPS_CLIENT_ID || '',
    clientSecret: env.OPS_CLIENT_SECRET || '',
    redirectUri: env.OPS_REDIRECT_URI || '',
    callbackPath: env.OPS_REDIRECT_URI ? new URL(env.OPS_REDIRECT_URI).pathname : '/ops/auth/callback',
    authorizeUrl: env.OPS_AUTHORIZE_URL || '',
    tokenUrl: env.OPS_TOKEN_URL || '',
    userinfoUrl: env.OPS_USERINFO_URL || '',
    scope: env.OPS_SCOPE || 'openid profile',
    sessionSecret: env.SESSION_SECRET || '',
    sessionTtl: Number(env.SESSION_TTL_SEC || 28800),
    stateTtl: Number(env.STATE_TTL_SEC || 600),
    cookieSecure: env.COOKIE_SECURE !== 'false',
    cookieSameSite: env.COOKIE_SAME_SITE || 'Lax',
    mcpUrl: env.OPS_SITE_MCP_URL || '',
    mcpKey: env.OPS_SITE_MCP_KEY || '',
    aiUrl: (env.OPS_SITE_AI_URL || '').replace(/\/+$/, ''),
    aiKey: env.OPS_SITE_AI_KEY || '',
    skillsUrl: env.OPS_SITE_SKILLS_URL || '',
    quotaRequestUrl: env.OPS_QUOTA_REQUEST_URL || '',
  };
}

export function features(cfg) {
  return {
    mcp: Boolean(cfg.mcpUrl && cfg.mcpKey),
    ai: Boolean(cfg.aiUrl && cfg.aiKey),
    skills: Boolean(cfg.skillsUrl && cfg.aiKey),
  };
}

function cookieOpts(cfg, maxAge) {
  return { secure: cfg.cookieSecure, sameSite: cfg.cookieSameSite, httpOnly: true, path: '/', maxAge };
}

export async function getSession(req, cfg) {
  const cookies = parseCookies(req.headers.get('cookie'));
  const raw = cookies[SID_COOKIE];
  if (!raw) return null;
  const s = await unsign(cfg.sessionSecret, raw);
  if (!s || !s.sub) return null;
  return s;
}

export function buildAuthorizeUrl(cfg, state) {
  const u = new URL(cfg.authorizeUrl);
  u.searchParams.set('response_type', 'code');
  u.searchParams.set('client_id', cfg.clientId);
  u.searchParams.set('redirect_uri', cfg.redirectUri);
  u.searchParams.set('scope', cfg.scope);
  u.searchParams.set('state', state);
  return u.toString();
}

async function exchangeCode(cfg, code) {
  const form = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: cfg.clientId,
    client_secret: cfg.clientSecret,
    code,
    redirect_uri: cfg.redirectUri,
  });
  const res = await timeoutFetch(cfg.tokenUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
    body: form.toString(),
  }, 15000);
  const text = await res.text();
  if (!res.ok) return { ok: false, error: describeUpstream(res, text) };
  let j;
  try { j = JSON.parse(text); } catch { return { ok: false, error: { status: res.status, detail: 'token 响应非 JSON' } }; }
  if (!j.access_token) return { ok: false, error: { status: res.status, detail: '缺少 access_token' } };
  return { ok: true, accessToken: j.access_token, expiresIn: Number(j.expires_in) || null };
}

async function fetchUserinfo(cfg, accessToken) {
  const res = await timeoutFetch(cfg.userinfoUrl, {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  }, 15000);
  const text = await res.text();
  if (!res.ok) return { ok: false, error: describeUpstream(res, text) };
  let j;
  try { j = JSON.parse(text); } catch { return { ok: false, error: { status: res.status, detail: 'userinfo 非 JSON' } }; }
  const u = j && j.sub ? j : (j.data || j);
  if (!u || !u.sub) return { ok: false, error: { status: res.status, detail: '缺少 sub' } };
  return {
    ok: true,
    user: {
      sub: String(u.sub),
      username: u.preferred_username || u.username || '',
      name: u.name || u.preferred_username || '',
      avatar: u.avatar || '',
      email: u.email || '',
    },
  };
}

// ---------------------------------------------------------------- 路由

export async function handleLogin(req, cfg) {
  const url = new URL(req.url);
  const next = safeNext(url.searchParams.get('next'), '/');
  const now = Math.floor(Date.now() / 1000);
  const state = await sign(cfg.sessionSecret, { n: randomToken(16), next, exp: now + cfg.stateTtl });

  return redirectResponse(buildAuthorizeUrl(cfg, state), [
    serializeCookie(STATE_COOKIE, state, cookieOpts(cfg, cfg.stateTtl)),
  ]);
}

export async function handleCallback(req, cfg) {
  const url = new URL(req.url);
  const q = url.searchParams;

  // 1) 错误参数优先
  if (q.get('error')) {
    const desc = q.get('error_description') || '';
    return htmlResponse(errorPage({
      title: '授权未完成',
      detail: `智能中枢返回：${q.get('error')}${desc ? '（' + desc + '）' : ''}`,
      code: 'authorize_error',
    }), 400);
  }

  // 2) state 校验（签名 + 期限 + 绑定当前浏览器）
  const state = q.get('state');
  const cookies = parseCookies(req.headers.get('cookie'));
  const stateCookie = cookies[STATE_COOKIE];
  if (!state) {
    return htmlResponse(errorPage({ detail: '回调缺少 state 参数。', code: 'state_missing' }), 400);
  }
  if (!stateCookie) {
    return htmlResponse(errorPage({
      detail: '浏览器缺少 state Cookie，可能换过浏览器或清过 Cookie。请重新登录。',
      code: 'state_cookie_missing',
    }), 400);
  }
  if (!safeEqual(state, stateCookie)) {
    return htmlResponse(errorPage({
      detail: 'state 与当前浏览器不匹配，已拒绝。请重新登录。',
      code: 'state_mismatch',
    }), 400);
  }
  const st = await unsign(cfg.sessionSecret, state);
  if (!st) {
    return htmlResponse(errorPage({
      detail: 'state 无效或已过期。请重新登录。',
      code: 'state_invalid',
    }), 400);
  }

  const code = q.get('code');
  if (!code) {
    return htmlResponse(errorPage({ detail: '回调缺少 code 参数。', code: 'code_missing' }), 400);
  }

  // 3) 换 token（必须后端做，带 client_secret）
  if (!cfg.clientSecret) {
    return htmlResponse(errorPage({
      title: '站点未配置完成',
      detail: '服务端缺少 client_secret，无法完成登录。请联系管理员。',
      code: 'server_misconfig',
    }), 500);
  }

  let tok;
  try {
    tok = await exchangeCode(cfg, code);
  } catch {
    return htmlResponse(errorPage({ detail: '与智能中枢通信失败，请稍后重试。', code: 'token_unreachable' }), 502);
  }
  if (!tok.ok) {
    return htmlResponse(errorPage({
      detail: '授权码校验失败。授权码 5 分钟内有效且只能使用一次，请重新登录。',
      code: 'token_rejected',
    }), 400);
  }

  // 4) 取身份
  let info;
  try {
    info = await fetchUserinfo(cfg, tok.accessToken);
  } catch {
    return htmlResponse(errorPage({ detail: '获取用户信息失败，请稍后重试。', code: 'userinfo_unreachable' }), 502);
  }
  if (!info.ok) {
    return htmlResponse(errorPage({ detail: '获取用户信息失败，请稍后重试。', code: 'userinfo_rejected' }), 502);
  }

  // 5) 建会话（签名 Cookie，每次登录都是新的）
  const now = Math.floor(Date.now() / 1000);
  const session = await sign(cfg.sessionSecret, {
    ...info.user,
    csrf: randomToken(24),
    iat: now,
    exp: now + cfg.sessionTtl,
  });

  return redirectResponse(safeNext(st.next, '/'), [
    serializeCookie(SID_COOKIE, session, cookieOpts(cfg, cfg.sessionTtl)),
    clearCookie(STATE_COOKIE, cookieOpts(cfg, 0)),
  ]);
}

export async function handleLogoutGet(req, cfg, session) {
  if (!session) return redirectResponse('/');
  const html = page('确认退出', `
      <h1>确认退出登录？</h1>
      <p>退出后需要重新通过智能中枢登录才能访问看板。</p>
      <form method="POST" action="/ops/auth/logout">
        <input type="hidden" name="csrf" value="${esc(session.csrf)}">
        <button class="btn" type="submit">退出登录</button>
      </form>
      <p style="margin-top:14px"><a href="/" style="font-size:13px;color:var(--ink2)">返回看板</a></p>`);
  return htmlResponse(html);
}

export async function handleLogoutPost(req, cfg, session) {
  const body = new URLSearchParams(await req.text());
  const csrf = body.get('csrf') || '';
  if (!session || !csrf || !safeEqual(csrf, session.csrf)) {
    return htmlResponse(errorPage({
      title: '请求被拒绝',
      detail: '退出请求未通过安全校验。请回到看板后重新点击退出。',
      code: 'csrf_failed',
    }), 403);
  }
  return redirectResponse('/', [clearCookie(SID_COOKIE, cookieOpts(cfg, 0))]);
}

export { loginRequired };
