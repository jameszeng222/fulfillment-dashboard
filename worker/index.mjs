/**
 * 智能中枢接入站点 —— Cloudflare Worker 入口。
 *
 * 职责：
 *   · 被登录保护的单文件看板（静态资源由 ASSETS 提供，登录通过后原样返回+注入身份条）
 *   · OAuth 登录 / 会话 / 退出
 *   · MCP 工具代理、AI 对话代理、技能列表
 *
 * 会话用 HMAC 签名 Cookie，服务端无状态，不依赖 KV / Durable Objects。
 */

import {
  loadConfig, features,
  getSession, handleLogin, handleCallback,
  handleLogoutGet, handleLogoutPost,
} from './ops.mjs';
import {
  UpstreamError, mcpListTools, mcpCallTool, flattenToolResult,
  aiListModels, aiChat, skillsList, skillLoad, skillReportUsage,
} from './integrations.mjs';
import { jsonResponse, htmlResponse, esc, safeEqual } from './util.mjs';

const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'SAMEORIGIN',
  'Referrer-Policy': 'same-origin',
  'X-XSS-Protection': '0',
  'Content-Security-Policy': [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'self'",
    "base-uri 'none'",
    "form-action 'self'",
  ].join('; '),
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
};

function withHeaders(res) {
  const out = new Response(res.body, res);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) out.headers.set(k, v);
  return out;
}

export default {
  async fetch(request, env, ctx) {
    let res;
    try {
      res = await route(request, env);
    } catch (e) {
      if (e instanceof UpstreamError) {
        res = jsonResponse({ error: e.code, message: e.message, detail: e.detail || '' }, clampStatus(e.status));
      } else {
        console.error('未捕获异常', e && e.stack ? e.stack : String(e));
        res = jsonResponse({ error: 'internal_error', message: '服务器内部错误' }, 500);
      }
    }
    return withHeaders(res);
  },
};

function clampStatus(s) {
  const n = Number(s) || 502;
  return Math.min(Math.max(n, 400), 599);
}

async function route(request, env) {
  const url = new URL(request.url);
  const p = url.pathname;
  const cfg = loadConfig(env);

  if (p === '/healthz') {
    return new Response(cfg.ok ? 'ok' : `missing:${cfg.missing.join(',')}`, {
      status: cfg.ok ? 200 : 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }

  // 配置不全时，除 healthz 外都给出明确提示，而不是白屏
  if (!cfg.ok) {
    return htmlResponse(
      `<!DOCTYPE html><meta charset="utf-8"><body style="font:15px/1.8 system-ui;padding:40px">
       <h2>站点未配置完成</h2><p>缺少环境变量：<code>${esc(cfg.missing.join(', '))}</code></p>
       <p>用 <code>wrangler secret put</code> 设置后重新部署。</p></body>`,
      503
    );
  }

  // ---- OAuth ----
  if (p === cfg.callbackPath && request.method === 'GET') return handleCallback(request, cfg);
  if (p === '/ops/auth/login' && request.method === 'GET') return handleLogin(request, cfg);
  if (p === '/ops/auth/logout') {
    const session = await getSession(request, cfg);
    if (request.method === 'GET') return handleLogoutGet(request, cfg, session);
    if (request.method === 'POST') return handleLogoutPost(request, cfg, session);
    return new Response('Method Not Allowed', { status: 405 });
  }

  const session = await getSession(request, cfg);

  // ---- API ----
  if (p.startsWith('/api/')) return apiRoute(request, url, cfg, session, env);

  // ---- 首页（需登录） ----
  if (p === '/') {
    if (!session) {
      const next = url.searchParams.get('next') || '/';
      const { loginRequired } = await import('./ops.mjs');
      return htmlResponse(loginRequired(next));
    }
    const assetRes = await env.ASSETS.fetch(new Request(new URL('/index.html', url), request));
    if (!assetRes.ok) {
      return htmlResponse(
        `<!DOCTYPE html><meta charset="utf-8"><body style="font:15px/1.8 system-ui;padding:40px">
         <h2>看板尚未构建</h2><p>先执行 <code>python assemble.py</code> 生成 public/index.html 再部署。</p></body>`,
        503
      );
    }
    const html = await assetRes.text();
    return htmlResponse(injectAuthBar(html, session));
  }

  // ---- 其余静态资源 ----
  return env.ASSETS.fetch(request);
}

async function apiRoute(request, url, cfg, session, env) {
  const p = url.pathname;
  const feats = features(cfg);

  if (!session) return jsonResponse({ error: 'unauthenticated', message: '请先登录' }, 401);

  if (request.method !== 'GET') {
    const csrf = request.headers.get('x-csrf-token') || '';
    if (!csrf || !safeEqual(csrf, session.csrf)) {
      return jsonResponse({ error: 'csrf_failed', message: '安全校验失败，请刷新页面重试' }, 403);
    }
    const origin = request.headers.get('origin');
    if (origin && origin !== cfg.origin) {
      return jsonResponse({ error: 'origin_mismatch', message: '来源校验失败' }, 403);
    }
  }

  if (p === '/api/me' && request.method === 'GET') {
    return jsonResponse({
      user: { sub: session.sub, username: session.username, name: session.name, avatar: session.avatar },
      csrf: session.csrf,
      features: feats,
      quotaRequestUrl: cfg.quotaRequestUrl,
      engine: 'logistics-dashboard',
    });
  }

  // ---- MCP ----
  if (p === '/api/mcp/tools' && request.method === 'GET') {
    if (!feats.mcp) return jsonResponse({ error: 'mcp_disabled', message: '站点 MCP 未配置' }, 503);
    return jsonResponse({ tools: await mcpListTools(cfg) });
  }

  if (p === '/api/mcp/call' && request.method === 'POST') {
    if (!feats.mcp) return jsonResponse({ error: 'mcp_disabled', message: '站点 MCP 未配置' }, 503);
    const body = await readJson(request);
    if (!body || !body.name) return jsonResponse({ error: 'bad_request', message: '缺少工具名' }, 400);
    const result = await mcpCallTool(cfg, body.name, body.arguments || {});
    return jsonResponse({ ok: !(result && result.isError), text: flattenToolResult(result), raw: result });
  }

  // ---- AI ----
  if (p === '/api/ai/models' && request.method === 'GET') {
    if (!feats.ai) return jsonResponse({ error: 'ai_disabled', message: '站点 AI 未配置' }, 503);
    return jsonResponse({ models: await aiListModels(cfg) });
  }

  if (p === '/api/ai/chat' && request.method === 'POST') {
    if (!feats.ai) return jsonResponse({ error: 'ai_disabled', message: '站点 AI 未配置' }, 503);
    const body = await readJson(request);
    const messages = Array.isArray(body?.messages) ? body.messages : null;
    if (!messages || !messages.length) return jsonResponse({ error: 'bad_request', message: '缺少 messages' }, 400);
    const out = await aiChat(cfg, {
      model: body.model,
      messages: messages.slice(-20).map((m) => ({
        role: m.role === 'assistant' ? 'assistant' : 'user',
        content: String(m.content || '').slice(0, 8000),
      })),
    });
    return jsonResponse(out);
  }

  // ---- 技能 ----
  if (p === '/api/skills' && request.method === 'GET') {
    if (!feats.skills) return jsonResponse({ error: 'skills_disabled', message: '技能服务未配置' }, 503);
    const items = await skillsList(cfg);
    return jsonResponse({
      skills: items.map((s) => ({ id: s.id, name: s.name, description: s.description, version: s.version })),
    });
  }

  if (p === '/api/skills/usage' && request.method === 'POST') {
    if (!feats.skills) return jsonResponse({ error: 'skills_disabled', message: '技能服务未配置' }, 503);
    const body = await readJson(request);
    const ids = Array.isArray(body?.ids) ? body.ids.filter((x) => typeof x === 'string').slice(0, 20) : [];
    if (!ids.length) return jsonResponse({ error: 'bad_request', message: '缺少 ids' }, 400);
    return jsonResponse(await skillReportUsage(cfg, ids, body.eventKey));
  }

  const m = p.match(/^\/api\/skills\/([^/]+)\/instructions$/);
  if (m && request.method === 'GET') {
    if (!feats.skills) return jsonResponse({ error: 'skills_disabled', message: '技能服务未配置' }, 503);
    const id = decodeURIComponent(m[1]);
    return jsonResponse({ id, instructions: await skillLoad(cfg, id) });
  }

  return jsonResponse({ error: 'not_found' }, 404);
}

async function readJson(request, limit = 256 * 1024) {
  const text = await request.text();
  if (text.length > limit) throw new UpstreamError('payload_too_large', '请求体过大', { status: 413 });
  if (!text) return {};
  try { return JSON.parse(text); } catch { throw new UpstreamError('bad_request', '请求体不是合法 JSON', { status: 400 }); }
}

/** 右下角身份条：显示当前账号、入口到 AI 助手、退出登录 */
function injectAuthBar(html, session) {
  const display = session.name || session.username || session.sub;
  const av = session.avatar
    ? `<img src="${esc(session.avatar)}" alt="" style="width:22px;height:22px;border-radius:50%;object-fit:cover">`
    : '';
  const bar = `
<div id="ops-authbar" style="position:fixed;right:18px;bottom:18px;z-index:99999;
  display:flex;align-items:center;gap:10px;background:#fff;border:1px solid #e6ebf2;
  border-radius:999px;padding:6px 8px 6px 12px;box-shadow:0 6px 24px rgba(20,40,80,.14);
  font:12.5px/1.2 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1b2430">
  ${av}
  <span style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${esc(display)}">${esc(display)}</span>
  <a href="/assistant.html" style="text-decoration:none;color:#2F5597;font-weight:600;padding:4px 8px;
     border-radius:999px;background:#f1f5fd">AI 助手</a>
  <a href="/ops/auth/logout" style="text-decoration:none;color:#5b6675;padding:4px 8px">退出</a>
</div>`;
  if (html.includes('</body>')) return html.replace('</body>', bar + '\n</body>');
  return html + bar;
}
