'use strict';
/**
 * 智能中枢 OAuth 授权码流程。
 *
 * 流程要点（对应需求 1~4）：
 *   1. 后端生成随机 state，绑定当前浏览器会话，带 response_type/client_id/redirect_uri/scope/state 跳转
 *   2. 回调先校验 error 与 state，再由后端 POST 换 token（携带 client_secret，5 分钟内单次使用）
 *   3. 用 access_token 调 userinfo，以 sub 作为稳定标识；不假设存在 id_token/refresh_token
 *   4. 建服务端会话 + HttpOnly/SameSite/Secure Cookie，退出、错误页、CSRF、安全回跳
 */

const crypto = require('crypto');
const http = require('./http');
const views = require('./views');
const { Sessions, safeEqual } = require('./session');
const { timeoutFetch, safeNext, describeUpstream } = require('./util');

const PRE_COOKIE = 'ops_pre';   // 登录前：只用来绑定 state，不具备任何权限
const SID_COOKIE = 'ops_sid';   // 登录后：服务端会话 id

function cookieOpts(cfg) {
  return { secure: cfg.cookieSecure, sameSite: cfg.cookieSameSite, httpOnly: true, path: '/' };
}

function buildAuthorizeUrl(cfg, state) {
  const u = new URL(cfg.authorizeUrl);
  u.searchParams.set('response_type', 'code');
  u.searchParams.set('client_id', cfg.clientId);
  u.searchParams.set('redirect_uri', cfg.redirectUri);
  u.searchParams.set('scope', cfg.scope);
  u.searchParams.set('state', state);
  return u.toString();
}

/** 后端换 token。按需求用表单提交；上游两种都接受。 */
async function exchangeCode(cfg, code) {
  const form = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: cfg.clientId,
    client_secret: cfg.clientSecret,
    code,
    redirect_uri: cfg.redirectUri, // 必须与登录时及登记值完全一致
  });

  const res = await timeoutFetch(cfg.tokenUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'application/json',
    },
    body: form.toString(),
  }, 15000);

  const text = await res.text();
  if (!res.ok) {
    return { ok: false, error: describeUpstream(res, text) };
  }
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    return { ok: false, error: { status: res.status, detail: 'token 响应不是 JSON' } };
  }
  if (!json.access_token) {
    return { ok: false, error: { status: res.status, detail: 'token 响应缺少 access_token' } };
  }
  return {
    ok: true,
    accessToken: json.access_token,
    tokenType: json.token_type || 'Bearer',
    expiresIn: Number(json.expires_in) || null,
    scope: json.scope || '',
  };
}

async function fetchUserinfo(cfg, accessToken) {
  const res = await timeoutFetch(cfg.userinfoUrl, {
    method: 'GET',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  }, 15000);

  const text = await res.text();
  if (!res.ok) return { ok: false, error: describeUpstream(res, text) };

  let json;
  try {
    json = JSON.parse(text);
  } catch {
    return { ok: false, error: { status: res.status, detail: 'userinfo 响应不是 JSON' } };
  }
  // 有些实现把用户放在 data 里
  const u = json && typeof json === 'object' && json.sub ? json : (json.data || json);
  if (!u || !u.sub) {
    return { ok: false, error: { status: res.status, detail: 'userinfo 缺少 sub 字段' } };
  }
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

function handleLogin(req, res, url, ctx) {
  const { cfg, sessions, log } = ctx;
  const cookies = http.parseCookies(req.headers.cookie);

  // 没有登录前标识就发一个；它就是 state 的绑定对象，不含任何身份信息
  let preId = cookies[PRE_COOKIE];
  const fresh = !preId;
  if (!preId || !/^[A-Za-z0-9_-]{20,64}$/.test(preId)) preId = crypto.randomBytes(24).toString('base64url');

  const redirectTo = safeNext(url.searchParams.get('next'), '/');
  const state = sessions.issueState({ sid: preId, redirectTo });
  const location = buildAuthorizeUrl(cfg, state);

  const headers = { Location: location, 'Cache-Control': 'no-store' };
  if (fresh) {
    headers['Set-Cookie'] = http.serializeCookie(PRE_COOKIE, preId, {
      ...cookieOpts(cfg),
      maxAge: cfg.stateTtlSec,
    });
  }
  res.writeHead(303, headers);
  res.end();
  log.info(`login: state 已签发 -> authorize (next=${redirectTo})`);
}

async function handleCallback(req, res, url, ctx) {
  const { cfg, sessions, log } = ctx;
  const q = url.searchParams;

  // ---- 1) 先看错误参数（用户拒绝授权、中枢报错） ----
  const err = q.get('error');
  if (err) {
    const desc = q.get('error_description') || '';
    log.warn(`callback: 授权端返回错误 ${err} ${desc}`);
    return sendError(res, cfg, {
      status: 400,
      title: '授权未完成',
      detail: `智能中枢返回：${err}${desc ? '（' + desc + '）' : ''}`,
      code: 'authorize_error',
    });
  }

  // ---- 2) 校验 state：必须存在、未过期、未被用过、属于当前浏览器 ----
  const state = q.get('state');
  const cookies = http.parseCookies(req.headers.cookie);
  const preId = cookies[PRE_COOKIE];

  if (!state) {
    return sendError(res, cfg, { status: 400, title: '登录失败', detail: '回调缺少 state 参数。', code: 'state_missing' });
  }
  if (!preId) {
    return sendError(res, cfg, {
      status: 400, title: '登录失败',
      detail: '浏览器缺少登录前标识，可能换过浏览器或清过 Cookie。请重新登录。',
      code: 'pre_cookie_missing',
    });
  }
  const rec = sessions.consumeState(state, preId);
  if (!rec) {
    return sendError(res, cfg, {
      status: 400, title: '登录失败',
      detail: 'state 无效、已过期或已被使用。请重新登录。',
      code: 'state_invalid',
    });
  }
  if (rec.mismatch) {
    log.warn('callback: state 与当前浏览器不匹配，已拒绝');
    return sendError(res, cfg, {
      status: 400, title: '登录失败',
      detail: 'state 与当前浏览器会话不匹配，已拒绝。请重新登录。',
      code: 'state_mismatch',
    });
  }

  const code = q.get('code');
  if (!code) {
    return sendError(res, cfg, { status: 400, title: '登录失败', detail: '回调缺少 code 参数。', code: 'code_missing' });
  }

  // ---- 3) 后端换 token ----
  if (!cfg.clientSecret) {
    log.error('callback: 未配置 OPS_CLIENT_SECRET，无法换 token');
    return sendError(res, cfg, {
      status: 500, title: '站点未配置完成',
      detail: '服务端缺少 client_secret，无法完成登录。请联系管理员。',
      code: 'server_misconfig',
    });
  }

  let tok;
  try {
    tok = await exchangeCode(cfg, code);
  } catch (e) {
    log.error(`callback: 换 token 请求异常 ${e.message}`);
    return sendError(res, cfg, {
      status: 502, title: '登录失败', detail: '与智能中枢通信失败，请稍后重试。', code: 'token_unreachable',
    });
  }
  if (!tok.ok) {
    log.error(`callback: 换 token 被拒 HTTP ${tok.error.status} ${tok.error.detail}`);
    return sendError(res, cfg, {
      status: 400, title: '登录失败',
      detail: '授权码校验失败。授权码 5 分钟内有效且只能使用一次，请重新登录。',
      code: 'token_rejected',
    });
  }

  // ---- 4) 取身份 ----
  let info;
  try {
    info = await fetchUserinfo(cfg, tok.accessToken);
  } catch (e) {
    log.error(`callback: userinfo 请求异常 ${e.message}`);
    return sendError(res, cfg, {
      status: 502, title: '登录失败', detail: '获取用户信息失败，请稍后重试。', code: 'userinfo_unreachable',
    });
  }
  if (!info.ok) {
    log.error(`callback: userinfo 被拒 HTTP ${info.error.status} ${info.error.detail}`);
    return sendError(res, cfg, {
      status: 502, title: '登录失败', detail: '获取用户信息失败，请稍后重试。', code: 'userinfo_rejected',
    });
  }

  // ---- 5) 建会话（换新的 sid，避免会话固定攻击） ----
  const { sid, csrf } = sessions.createSession(info.user);
  const headers = { 'Cache-Control': 'no-store' };
  headers['Set-Cookie'] = [
    http.serializeCookie(SID_COOKIE, sid, { ...cookieOpts(cfg), maxAge: cfg.sessionTtlSec }),
    http.clearCookie(PRE_COOKIE, cookieOpts(cfg)),
  ];

  // 回跳目标只接受站内相对路径
  const target = safeNext(rec.redirectTo, '/');
  res.writeHead(303, { ...headers, Location: target });
  res.end();
  log.info(`callback: 登录成功 sub=${info.user.sub} -> ${target}`);
}

function handleLogoutGet(req, res, ctx) {
  const { sessions } = ctx;
  const cookies = http.parseCookies(req.headers.cookie);
  const sess = sessions.get(cookies[SID_COOKIE]);
  if (!sess) {
    res.writeHead(303, { Location: '/' });
    return res.end();
  }
  const html = views.logoutConfirm({ csrf: sess.csrf });
  res.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': Buffer.byteLength(html),
    'Cache-Control': 'no-store',
  });
  res.end(html);
}

function handleLogoutPost(req, res, ctx, body) {
  const { cfg, sessions, log } = ctx;
  const cookies = http.parseCookies(req.headers.cookie);
  const sid = cookies[SID_COOKIE];
  const sess = sessions.get(sid);

  // CSRF：表单里的 token 必须与会话里的一致
  const csrf = body.get('csrf') || '';
  if (!sess || !csrf || !safeEqual(csrf, sess.csrf)) {
    log.warn('logout: CSRF 校验失败');
    return sendError(res, cfg, {
      status: 403, title: '请求被拒绝',
      detail: '退出请求未通过安全校验。请回到看板后重新点击退出。',
      code: 'csrf_failed',
    });
  }
  sessions.destroy(sid);
  res.writeHead(303, {
    Location: '/',
    'Set-Cookie': http.clearCookie(SID_COOKIE, cookieOpts(cfg)),
    'Cache-Control': 'no-store',
  });
  res.end();
  log.info(`logout: sub=${sess.user.sub} 已退出`);
}

function sendError(res, cfg, opts) {
  const html = views.errorPage(opts);
  res.writeHead(opts.status || 400, {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': Buffer.byteLength(html),
    'Cache-Control': 'no-store',
  });
  res.end(html);
}

module.exports = {
  PRE_COOKIE, SID_COOKIE, cookieOpts,
  buildAuthorizeUrl, exchangeCode, fetchUserinfo,
  handleLogin, handleCallback, handleLogoutGet, handleLogoutPost,
};
