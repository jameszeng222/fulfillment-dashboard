'use strict';
/**
 * 智慧中枢接入站点 —— 入口。
 *
 * 一个进程同时提供：
 *   · 被登录保护的单文件看板（public/index.html，构建产物，业务逻辑不动）
 *   · 智能中枢 OAuth 登录 / 会话 / 退出
 *   · MCP 工具代理、AI 对话代理、技能列表
 *
 * 零 npm 依赖，只要求 Node 18+（用了内置 fetch）。
 */

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const http = require('node:http');

const { load: loadConfig, summarize } = require('./config');
const H = require('./http');
const { makeLogger, safeNext } = require('./util');
const { Sessions, safeEqual } = require('./session');
const oauth = require('./oauth');
const views = require('./views');
const { McpClient, McpError, flattenToolResult } = require('./mcp');
const { AiClient, AiError } = require('./ai');
const { Skills, SkillError } = require('./skills');

const ROOT = path.resolve(__dirname, '..');
const PUBLIC_DIR = path.join(ROOT, 'public');
const SKILL_CACHE = path.join(__dirname, '.skill-cache');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};

function main() {
  let cfg;
  try {
    cfg = loadConfig();
  } catch (e) {
    // 配置不全就别启动，避免半配置状态下跑出安全问题
    console.error(`[配置错误] ${e.message}`);
    if (e.missing) console.error(`请设置：${e.missing.join(', ')}`);
    console.error('可参考 .env.example');
    process.exit(1);
  }

  const log = makeLogger('[ops] ');
  const sessions = new Sessions(cfg);

  const ctx = { cfg, sessions, log };

  const mcp = cfg.features.mcp ? new McpClient({ url: cfg.mcpUrl, key: cfg.mcpKey, log }) : null;
  const ai = cfg.features.ai
    ? new AiClient({ baseUrl: cfg.aiUrl, key: cfg.aiKey, log, defaultProtocol: process.env.OPS_AI_PROTOCOL || 'chat' })
    : null;
  const skills = cfg.features.skills
    ? new Skills({ url: cfg.skillsUrl, key: cfg.aiKey, log, cacheDir: SKILL_CACHE })
    : null;

  console.log(summarize(cfg));

  const server = http.createServer(async (req, res) => {
    res.req = req;
    H.securityHeaders(res);
    const t0 = Date.now();
    try {
      await route(req, res, ctx, { mcp, ai, skills });
    } catch (e) {
      const status = e && e.status ? e.status : 500;
      if (status >= 500) log.error(`${req.method} ${req.url} 未捕获异常 ${e.stack || e.message}`);
      if (!res.headersSent) H.sendJson(res, status, { error: 'internal_error', message: '服务器内部错误' });
      else res.end();
    } finally {
      const ms = Date.now() - t0;
      if (req.url && !req.url.startsWith('/api/')) log.info(`${req.method} ${req.url} ${res.statusCode} ${ms}ms`);
    }
  });

  server.listen(cfg.port, cfg.host, () => {
    log.info(`监听 http://${cfg.host}:${cfg.port}`);
  });

  const shutdown = (sig) => {
    log.info(`收到 ${sig}，正在退出`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5000).unref();
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

// ------------------------------------------------------------------ 路由

async function route(req, res, ctx, deps) {
  const { cfg } = ctx;
  const url = new URL(req.url, cfg.origin);
  const p = url.pathname;

  // 健康检查：给反代/监控用，不暴露任何内部信息
  if (p === '/healthz') return H.sendText(res, 200, 'ok');

  // ---- OAuth ----
  if (p === cfg.callbackPath && req.method === 'GET') return oauth.handleCallback(req, res, url, ctx);
  if (p === '/ops/auth/login' && req.method === 'GET') return oauth.handleLogin(req, res, url, ctx);
  if (p === '/ops/auth/logout') {
    if (req.method === 'GET') return oauth.handleLogoutGet(req, res, ctx);
    if (req.method === 'POST') {
      const body = await new URLSearchParams((await H.readBody(req)).toString('utf8'));
      return oauth.handleLogoutPost(req, res, ctx, body);
    }
    return H.sendText(res, 405, 'Method Not Allowed');
  }

  // ---- 从这里开始需要登录 ----
  const cookies = H.parseCookies(req.headers.cookie);
  const sess = ctx.sessions.get(cookies[oauth.SID_COOKIE]);

  if (p.startsWith('/api/')) return apiRoute(req, res, url, ctx, deps, sess);

  if (p === '/') {
    if (!sess) return sendHtml(res, views.loginRequired(url.searchParams.get('next') || '/'));
    return serveDashboard(req, res, ctx, sess);
  }

  // 静态资源（public 下的一切，但 index.html 只能走 / 且必须登录）
  return serveStatic(req, res, p);
}

async function apiRoute(req, res, url, ctx, deps, sess) {
  const { cfg } = ctx;
  const p = url.pathname;

  if (!sess) return H.sendJson(res, 401, { error: 'unauthenticated', message: '请先登录' });

  // 状态变更类请求统一做 CSRF + Origin 校验
  if (req.method !== 'GET') {
    const csrf = req.headers['x-csrf-token'] || '';
    if (!csrf || !safeEqual(csrf, sess.csrf)) {
      return H.sendJson(res, 403, { error: 'csrf_failed', message: '安全校验失败，请刷新页面重试' });
    }
    const origin = req.headers.origin;
    if (origin && origin !== cfg.origin) {
      return H.sendJson(res, 403, { error: 'origin_mismatch', message: '来源校验失败' });
    }
  }

  try {
    // ---- 当前身份与能力 ----
    if (p === '/api/me' && req.method === 'GET') {
      return H.sendJson(res, 200, {
        user: sess.user,
        csrf: sess.csrf,
        features: cfg.features,
        quotaRequestUrl: cfg.quotaRequestUrl,
        engine: 'logistics-dashboard',
      });
    }

    // ---- MCP ----
    if (p === '/api/mcp/tools' && req.method === 'GET') {
      if (!deps.mcp) return H.sendJson(res, 503, { error: 'mcp_disabled', message: '站点 MCP 未配置' });
      const tools = await deps.mcp.listTools({ force: url.searchParams.get('refresh') === '1' });
      return H.sendJson(res, 200, {
        tools: tools.map((t) => ({
          name: t.name,
          title: t.title || t.name,
          description: t.description || '',
          // 只把必要的输入结构给前端，避免把整份 schema 塞进页面
          inputSchema: t.inputSchema || null,
        })),
      });
    }

    if (p === '/api/mcp/call' && req.method === 'POST') {
      if (!deps.mcp) return H.sendJson(res, 503, { error: 'mcp_disabled', message: '站点 MCP 未配置' });
      const body = await H.readJson(req, 256 * 1024);
      const name = body && body.name;
      if (!name) return H.sendJson(res, 400, { error: 'bad_request', message: '缺少工具名' });

      // 白名单：只允许调用 tools/list 里真实存在且已授权的工具
      const tools = await deps.mcp.listTools();
      if (!tools.some((t) => t.name === name)) {
        return H.sendJson(res, 403, { error: 'tool_not_allowed', message: '该工具未授权' });
      }
      const result = await deps.mcp.callTool(name, body.arguments || {});
      return H.sendJson(res, 200, {
        ok: !(result && result.isError),
        text: flattenToolResult(result),
        raw: result,
      });
    }

    // ---- AI ----
    if (p === '/api/ai/models' && req.method === 'GET') {
      if (!deps.ai) return H.sendJson(res, 503, { error: 'ai_disabled', message: '站点 AI 未配置' });
      const models = await deps.ai.listModels({ force: url.searchParams.get('refresh') === '1' });
      return H.sendJson(res, 200, { models });
    }

    if (p === '/api/ai/chat' && req.method === 'POST') {
      if (!deps.ai) return H.sendJson(res, 503, { error: 'ai_disabled', message: '站点 AI 未配置' });
      const body = await H.readJson(req, 256 * 1024);
      const messages = Array.isArray(body.messages) ? body.messages : null;
      if (!messages || !messages.length) {
        return H.sendJson(res, 400, { error: 'bad_request', message: '缺少 messages' });
      }
      const out = await deps.ai.chat({
        model: body.model,
        messages: messages.slice(-20).map((m) => ({
          role: m.role === 'assistant' ? 'assistant' : 'user',
          content: String(m.content || '').slice(0, 8000),
        })),
      });
      return H.sendJson(res, 200, { text: out.text, model: out.model, protocol: out.protocol });
    }

    // ---- 技能 ----
    if (p === '/api/skills' && req.method === 'GET') {
      if (!deps.skills) return H.sendJson(res, 503, { error: 'skills_disabled', message: '技能服务未配置' });
      const items = await deps.skills.list({ force: url.searchParams.get('refresh') === '1' });
      return H.sendJson(res, 200, {
        skills: items.map((s) => ({ id: s.id, name: s.name, description: s.description, version: s.version })),
      });
    }

    if (p === '/api/skills/usage' && req.method === 'POST') {
      if (!deps.skills) return H.sendJson(res, 503, { error: 'skills_disabled', message: '技能服务未配置' });
      const body = await H.readJson(req, 64 * 1024);
      const ids = Array.isArray(body.ids) ? body.ids.filter((x) => typeof x === 'string').slice(0, 20) : [];
      if (!ids.length) return H.sendJson(res, 400, { error: 'bad_request', message: '缺少 ids' });
      const out = await deps.skills.reportUsage(ids, { eventKey: body.eventKey });
      return H.sendJson(res, 200, out);
    }

    // 下载 + sha256 校验 + 读取技能说明
    const mSkill = p.match(/^\/api\/skills\/([^/]+)\/instructions$/);
    if (mSkill && req.method === 'GET') {
      if (!deps.skills) return H.sendJson(res, 503, { error: 'skills_disabled', message: '技能服务未配置' });
      const id = decodeURIComponent(mSkill[1]);
      const instructions = await deps.skills.ensureLoaded(id, { force: url.searchParams.get('refresh') === '1' });
      return H.sendJson(res, 200, { id, instructions });
    }

    return H.sendJson(res, 404, { error: 'not_found' });
  } catch (e) {
    return sendUpstreamError(res, e, ctx);
  }
}

/** 把各类上游异常翻译成稳定的错误码 + 中文提示，不泄漏内部细节 */
function sendUpstreamError(res, e, ctx) {
  const known = e instanceof McpError || e instanceof AiError || e instanceof SkillError;
  const status = known ? Math.min(Math.max(e.httpStatus || 502, 400), 599) : 500;
  if (known) ctx.log.warn(`${e.code}: ${e.message} ${e.detail || ''}`);
  else ctx.log.error(`未预期的上游异常: ${e.stack || e.message}`);

  return H.sendJson(res, status, {
    error: known ? e.code : 'upstream_error',
    message: known ? e.message : '上游服务异常',
    detail: known ? (e.detail || '') : '',
  });
}

// ------------------------------------------------------------------ 静态资源

function serveStatic(req, res, pathname) {
  if (req.method !== 'GET' && req.method !== 'HEAD') return H.sendText(res, 405, 'Method Not Allowed');

  const rel = decodeURIComponent(pathname).replace(/^\/+/, '');
  if (!rel) return H.sendText(res, 404, 'Not Found');
  if (rel === 'index.html') {
    // index.html 必须登录，走 /
    return H.redirect(res, '/');
  }

  const abs = path.resolve(PUBLIC_DIR, rel);
  // 防目录穿越
  if (!abs.startsWith(PUBLIC_DIR + path.sep)) return H.sendText(res, 403, 'Forbidden');

  let st;
  try {
    st = fs.statSync(abs);
  } catch {
    return H.sendText(res, 404, 'Not Found');
  }
  if (!st.isFile()) return H.sendText(res, 404, 'Not Found');

  const type = MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream';
  const etag = `"${st.size.toString(16)}-${Math.floor(st.mtimeMs).toString(16)}"`;
  if (req.headers['if-none-match'] === etag) {
    res.writeHead(304, { ETag: etag });
    return res.end();
  }
  res.writeHead(200, {
    'Content-Type': type,
    'Content-Length': st.size,
    ETag: etag,
    'Cache-Control': 'no-cache',
  });
  if (req.method === 'HEAD') return res.end();
  fs.createReadStream(abs).pipe(res);
}

/** 看板：原样返回，只在末尾注入一个身份条，不改动看板本身任何逻辑 */
function serveDashboard(req, res, ctx, sess) {
  const file = path.join(PUBLIC_DIR, 'index.html');
  let html;
  try {
    html = fs.readFileSync(file, 'utf8');
  } catch {
    return sendHtml(res, views.errorPage({
      status: 503,
      title: '看板尚未构建',
      detail: '服务端还没有生成 public/index.html。请先执行 python assemble.py。',
      code: 'not_built',
    }));
  }
  const out = injectAuthBar(html, sess);
  res.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': Buffer.byteLength(out),
    'Cache-Control': 'no-store',
  });
  res.end(out);
}

/** 右下角身份条：显示当前账号、入口到 AI 助手、退出登录 */
function injectAuthBar(html, sess) {
  const u = sess.user || {};
  const display = u.name || u.username || u.sub;
  const av = u.avatar
    ? `<img src="${views.esc(u.avatar)}" alt="" style="width:22px;height:22px;border-radius:50%;object-fit:cover">`
    : '';
  const bar = `
<div id="ops-authbar" style="position:fixed;right:18px;bottom:18px;z-index:99999;
  display:flex;align-items:center;gap:10px;background:#fff;border:1px solid #e6ebf2;
  border-radius:999px;padding:6px 8px 6px 12px;box-shadow:0 6px 24px rgba(20,40,80,.14);
  font:12.5px/1.2 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1b2430">
  ${av}
  <span style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${views.esc(display)}">${views.esc(display)}</span>
  <a href="/assistant.html" style="text-decoration:none;color:#2F5597;font-weight:600;padding:4px 8px;
     border-radius:999px;background:#f1f5fd">AI 助手</a>
  <a href="/ops/auth/logout" style="text-decoration:none;color:#5b6675;padding:4px 8px">退出</a>
</div>`;
  if (html.includes('</body>')) return html.replace('</body>', bar + '\n</body>');
  return html + bar;
}

function sendHtml(res, html, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': Buffer.byteLength(html),
    'Cache-Control': 'no-store',
  });
  res.end(html);
}

if (require.main === module) main();

module.exports = { main, injectAuthBar, serveStatic, safeNext };
