/**
 * Worker 侧的 MCP / AI / 技能接入。只用 Web API。
 *
 * 与 Node 版的差异：
 *  · 没有跨请求内存 → MCP 每次请求内自己完成握手（幂等，代价是多两次往返）
 *  · 没有 zlib → 用 DecompressionStream('deflate-raw') 解压技能包
 *  · 没有 fs 缓存 → 技能包只在内存里验签后解析，不做磁盘持久化
 */

import { timeoutFetch, describeUpstream } from './util.mjs';

export class UpstreamError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.code = code;
    this.status = extra.status || 502;
    this.detail = extra.detail || '';
  }
}

// ------------------------------------------------------------------ MCP

const PROTOCOL = '2024-11-05';

function pickFromSse(text, id) {
  const out = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try { out.push(JSON.parse(payload)); } catch { /* 忽略 */ }
  }
  if (!out.length) return null;
  return out.find((m) => m && m.id === id) || out.find((m) => m && (m.result || m.error)) || null;
}

/** 在一个请求内完成 MCP 握手并调用若干方法 */
class McpSession {
  constructor(cfg) {
    this.cfg = cfg;
    this.sid = null;
    this.id = 0;
  }

  async post(payload) {
    const headers = {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      Authorization: `Bearer ${this.cfg.mcpKey}`,
    };
    if (this.sid) headers['Mcp-Session-Id'] = this.sid;

    const res = await timeoutFetch(this.cfg.mcpUrl, {
      method: 'POST', headers, body: JSON.stringify(payload),
    }, 20000);

    const s = res.headers.get('mcp-session-id');
    if (s) this.sid = s;

    const text = await res.text();
    if (!res.ok) {
      const info = describeUpstream(res, text);
      if (res.status === 401) throw new UpstreamError('MCP_UNAUTHORIZED', 'MCP 密钥无效或已被重置，请让管理员重新下发本站点 MCP 密钥。', { status: 401, detail: info.detail });
      if (res.status === 403) throw new UpstreamError('MCP_FORBIDDEN', '本站点没有调用该 MCP 工具的权限。', { status: 403, detail: info.detail });
      if (res.status === 404 || res.status === 405) throw new UpstreamError('MCP_DISABLED', '站点 MCP 未启用或地址不正确。', { status: res.status, detail: info.detail });
      throw new UpstreamError('MCP_UPSTREAM', 'MCP 服务返回异常。', { status: res.status, detail: info.detail });
    }
    if (payload.id == null || !text) return null;

    const ct = (res.headers.get('content-type') || '').toLowerCase();
    let body = null;
    if (ct.includes('text/event-stream')) body = pickFromSse(text, payload.id);
    else { try { body = JSON.parse(text); } catch { body = pickFromSse(text, payload.id); } }
    if (!body) throw new UpstreamError('MCP_BAD_RESPONSE', 'MCP 返回内容无法解析。', { detail: text.slice(0, 200) });
    if (body.error) throw new UpstreamError('MCP_RPC_ERROR', body.error.message || '工具调用失败。', { detail: String(body.error.code ?? '') });
    return body.result;
  }

  async init() {
    await this.post({
      jsonrpc: '2.0', id: ++this.id, method: 'initialize',
      params: { protocolVersion: PROTOCOL, capabilities: {}, clientInfo: { name: 'logistics-dashboard', version: '1.0.0' } },
    });
    await this.post({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
  }

  async listTools() {
    await this.init();
    const r = await this.post({ jsonrpc: '2.0', id: ++this.id, method: 'tools/list', params: {} });
    return (r && r.tools) || [];
  }

  async callTool(name, args) {
    await this.init();
    return this.post({ jsonrpc: '2.0', id: ++this.id, method: 'tools/call', params: { name, arguments: args || {} } });
  }
}

export async function mcpListTools(cfg) {
  const s = new McpSession(cfg);
  const tools = await s.listTools();
  return tools.map((t) => ({
    name: t.name, title: t.title || t.name, description: t.description || '',
    inputSchema: t.inputSchema || null,
  }));
}

export async function mcpCallTool(cfg, name, args) {
  const s = new McpSession(cfg);
  const tools = await s.listTools();
  if (!tools.some((t) => t.name === name)) {
    throw new UpstreamError('tool_not_allowed', '该工具未授权', { status: 403 });
  }
  return s.callTool(name, args);
}

export function flattenToolResult(result) {
  if (!result) return '';
  if (Array.isArray(result.content)) {
    return result.content.map((c) => {
      if (!c) return '';
      if (c.type === 'text') return c.text || '';
      if (c.type === 'json') return JSON.stringify(c.json);
      if (c.type === 'resource') return c.resource?.text || '';
      return `[${c.type || 'unknown'}]`;
    }).filter(Boolean).join('\n');
  }
  return typeof result === 'string' ? result : JSON.stringify(result);
}

// ------------------------------------------------------------------ AI

function looksLikeQuota(status, detail) {
  if (status === 402) return true;
  return /quota|额度|配额|余额|不足|exceeded|out of credit|insufficient/i.test(String(detail || ''));
}

async function aiReq(cfg, path, init) {
  const res = await timeoutFetch(`${cfg.aiUrl}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${cfg.aiKey}`, Accept: 'application/json', ...(init?.headers || {}) },
  }, 60000);
  return { res, text: await res.text() };
}

export async function aiListModels(cfg) {
  const { res, text } = await aiReq(cfg, '/models', { method: 'GET' });
  if (!res.ok) {
    const info = describeUpstream(res, text);
    if (looksLikeQuota(res.status, info.detail)) throw new UpstreamError('AI_QUOTA', 'AI 额度不足。', { status: res.status, detail: info.detail });
    if (res.status === 401 || res.status === 403) throw new UpstreamError('AI_UNAUTHORIZED', '站点 AI Key 无效或未授权。', { status: res.status, detail: info.detail });
    throw new UpstreamError('AI_UPSTREAM', '获取模型列表失败。', { status: res.status, detail: info.detail });
  }
  let j;
  try { j = JSON.parse(text); } catch { throw new UpstreamError('AI_BAD_RESPONSE', '模型列表不是 JSON。'); }
  const raw = Array.isArray(j) ? j : (j.data || j.models || []);
  return raw.map((m) => (typeof m === 'string'
    ? { id: m, protocol: '' }
    : { id: m.id || m.name || m.model || '', protocol: m.protocol || m.api_style || m.type || '' }))
    .filter((m) => m.id);
}

function resolveProtocol(model) {
  const p = String(model?.protocol || '').toLowerCase();
  if (p.includes('response')) return 'responses';
  if (p.includes('message') || p.includes('anthropic')) return 'messages';
  if (p.includes('chat')) return 'chat';
  const id = String(model?.id || '').toLowerCase();
  if (id.startsWith('claude')) return 'messages';
  return 'chat';
}

export async function aiChat(cfg, { model, messages, maxTokens = 1200 }) {
  const models = await aiListModels(cfg);
  const chosen = model ? (models.find((m) => m.id === model) || { id: model, protocol: '' }) : models[0];
  if (!chosen?.id) throw new UpstreamError('AI_NO_MODEL', '没有可用模型。');

  const protocol = resolveProtocol(chosen);
  let path, body;
  if (protocol === 'messages') {
    path = '/messages';
    body = { model: chosen.id, max_tokens: maxTokens, messages };
  } else if (protocol === 'responses') {
    path = '/responses';
    body = { model: chosen.id, max_output_tokens: maxTokens, input: messages };
  } else {
    path = '/chat/completions';
    body = { model: chosen.id, max_tokens: maxTokens, messages, stream: false };
  }

  const { res, text } = await aiReq(cfg, path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const info = describeUpstream(res, text);
    if (looksLikeQuota(res.status, info.detail)) throw new UpstreamError('AI_QUOTA', 'AI 额度不足，请申请增加额度。', { status: res.status, detail: info.detail });
    throw new UpstreamError('AI_UPSTREAM', 'AI 调用失败。', { status: res.status, detail: info.detail });
  }
  let j;
  try { j = JSON.parse(text); } catch { throw new UpstreamError('AI_BAD_RESPONSE', 'AI 返回内容不是 JSON。'); }

  let out = '';
  if (protocol === 'messages') out = (j.content || []).map((c) => c.text || '').join('');
  else if (protocol === 'responses') out = j.output_text || (j.output || []).flatMap((o) => o.content || []).map((c) => c.text || '').join('');
  else out = j.choices?.[0]?.message?.content || '';

  return { text: String(out).trim(), model: chosen.id, protocol };
}

// ------------------------------------------------------------------ 技能

async function sha256Hex(bytes) {
  const d = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function inflateRaw(bytes) {
  const ds = new DecompressionStream('deflate-raw');
  const stream = new Blob([bytes]).stream().pipeThrough(ds);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** 读 zip 中央目录，返回 [{name, data}]。支持 stored 与 deflate。 */
async function unzip(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const u16 = (o) => dv.getUint16(o, true);
  const u32 = (o) => dv.getUint32(o, true);

  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 0xffff); i--) {
    if (u32(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('找不到 zip 中央目录');

  const count = u16(eocd + 10);
  let p = u32(eocd + 16);
  const out = [];

  for (let i = 0; i < count && i < 500; i++) {
    if (p + 46 > bytes.length || u32(p) !== 0x02014b50) break;
    const method = u16(p + 10);
    const compSize = u32(p + 20);
    const nameLen = u16(p + 28);
    const extraLen = u16(p + 30);
    const commentLen = u16(p + 32);
    const localOff = u32(p + 42);
    const name = new TextDecoder().decode(bytes.slice(p + 46, p + 46 + nameLen));
    p += 46 + nameLen + extraLen + commentLen;
    if (name.endsWith('/')) continue;
    if (localOff + 30 > bytes.length || u32(localOff) !== 0x04034b50) continue;

    const lNameLen = u16(localOff + 26);
    const lExtraLen = u16(localOff + 28);
    const start = localOff + 30 + lNameLen + lExtraLen;
    const raw = bytes.slice(start, start + compSize);
    let data;
    if (method === 0) data = raw;
    else if (method === 8) { try { data = await inflateRaw(raw); } catch { continue; } }
    else continue;
    out.push({ name, data });
  }
  return out;
}

function normalizeSkill(s) {
  if (typeof s === 'string') return { id: s, name: s };
  return {
    id: s.id || s.key || s.skill_id || s.name || '',
    name: s.name || s.title || s.id || '',
    description: s.description || s.summary || s.desc || '',
    instructions: s.instructions || s.content || '',
    downloadPath: s.downloadPath || s.download_path || s.package || '',
    sha256: s.sha256 || s.checksum || '',
    version: s.version || '',
  };
}

export async function skillsList(cfg) {
  const res = await timeoutFetch(cfg.skillsUrl, {
    headers: { Authorization: `Bearer ${cfg.aiKey}`, Accept: 'application/json' },
  }, 30000);
  const text = await res.text();
  if (!res.ok) {
    const info = describeUpstream(res, text);
    if (res.status === 401 || res.status === 403) {
      throw new UpstreamError('SKILL_UNAUTHORIZED', '技能服务未授权。', { status: res.status, detail: info.detail });
    }
    throw new UpstreamError('SKILL_UPSTREAM', '获取技能列表失败。', { status: res.status, detail: info.detail });
  }
  let j;
  try { j = JSON.parse(text); } catch { throw new UpstreamError('SKILL_BAD_RESPONSE', '技能列表不是 JSON。'); }
  const arr = Array.isArray(j) ? j : (j.items || j.skills || j.data || []);
  return arr.map(normalizeSkill).filter((s) => s.id);
}

export async function skillLoad(cfg, id) {
  const items = await skillsList(cfg);
  const item = items.find((s) => s.id === id);
  if (!item) throw new UpstreamError('SKILL_NOT_FOUND', `未授权的技能：${id}`, { status: 404 });

  let text = item.instructions || '';

  if (item.downloadPath) {
    const abs = new URL(item.downloadPath, cfg.skillsUrl).toString();
    const res = await timeoutFetch(abs, { headers: { Authorization: `Bearer ${cfg.aiKey}` } }, 60000);
    if (!res.ok) throw new UpstreamError('SKILL_DOWNLOAD', `下载技能包失败：${id}`, { status: res.status });
    const buf = new Uint8Array(await res.arrayBuffer());
    if (buf.length > 32 * 1024 * 1024) throw new UpstreamError('SKILL_TOO_LARGE', '技能包体积超限。');

    if (item.sha256) {
      const got = await sha256Hex(buf);
      if (got.toLowerCase() !== String(item.sha256).toLowerCase()) {
        throw new UpstreamError('SKILL_HASH_MISMATCH', '技能包校验失败，已拒绝加载。');
      }
    }

    if (!text) {
      try {
        const entries = await unzip(buf);
        const md = entries.filter((e) => e.name.toLowerCase().endsWith('.md'));
        const pick = md.find((e) => /(^|\/)skill\.md$/i.test(e.name))
          || md.find((e) => /(^|\/)readme\.md$/i.test(e.name))
          || md[0];
        if (pick) text = new TextDecoder().decode(pick.data).slice(0, 200_000);
      } catch { /* 不是 zip 就跳过 */ }
    }
  }

  if (!text) text = item.description || '';
  return text;
}

export async function skillReportUsage(cfg, ids, eventKey) {
  const key = eventKey || `logistics-dashboard:${Date.now()}:${crypto.randomUUID().slice(0, 12)}`;
  const res = await timeoutFetch(`${cfg.skillsUrl}/usage`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${cfg.aiKey}`, 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ ids, eventKey: key, engine: 'logistics-dashboard' }),
  }, 20000);
  const text = await res.text();
  if (!res.ok) {
    const info = describeUpstream(res, text);
    throw new UpstreamError('SKILL_USAGE_FAILED', '使用记录上报失败。', { status: res.status, detail: info.detail });
  }
  return { ok: true, eventKey: key };
}
