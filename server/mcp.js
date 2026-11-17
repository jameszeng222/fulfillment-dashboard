'use strict';
/**
 * MCP 客户端（Streamable HTTP / JSON-RPC 2.0）。
 *
 * 对应需求 5：站点后端带 OPS_SITE_MCP_KEY 请求 OPS_SITE_MCP_URL，
 * 先 initialize、notifications/initialized，再 tools/list 发现工具，tools/call 调用。
 * 该密钥独立于用户个人 MCP 密钥，权限由本站点配置决定。
 *
 * 协议细节：
 *   - 请求头 Accept 同时给 application/json 与 text/event-stream
 *   - 响应可能是 JSON，也可能是 SSE（每行 data: <json>）
 *   - initialize 可能返回 Mcp-Session-Id，后续请求要带上
 *   - notifications/* 没有 id，服务端通常回 202 空体，不能当失败
 */

const { timeoutFetch, describeUpstream } = require('./util');

const PROTOCOL_VERSION = '2024-11-05';

class McpError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'McpError';
    this.code = code;         // 给前端看的稳定错误码
    this.httpStatus = extra.httpStatus || 502;
    this.detail = extra.detail || '';
  }
}

/** 从 SSE 正文里挑出与 id 匹配的那条 JSON-RPC 响应 */
function pickFromSse(text, id) {
  const out = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {
      out.push(JSON.parse(payload));
    } catch {
      /* 忽略非 JSON 的 data 行 */
    }
  }
  if (!out.length) return null;
  if (id == null) return out[0];
  return out.find((m) => m && m.id === id) || out.find((m) => m && (m.result || m.error)) || null;
}

class McpClient {
  constructor({ url, key, log }) {
    this.url = url;
    this.key = key;
    this.log = log;
    this.sessionId = null;
    this.inflight = null;
    this.toolsCache = null;
    this.toolsCachedAt = 0;
    this.nextId = 1;
  }

  /** 串行化初始化，避免并发重复握手 */
  async ensureReady() {
    if (this.sessionId) return;
    if (!this.inflight) {
      this.inflight = this.#initialize().finally(() => {
        this.inflight = null;
      });
    }
    await this.inflight;
  }

  async #post(payload) {
    const headers = {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      Authorization: `Bearer ${this.key}`,
    };
    if (this.sessionId) headers['Mcp-Session-Id'] = this.sessionId;

    const res = await timeoutFetch(this.url, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    }, 20000);

    // initialize 之后服务端可能下发会话 id
    const sid = res.headers.get('mcp-session-id');
    if (sid) this.sessionId = sid;

    const text = await res.text();

    if (!res.ok) {
      const info = describeUpstream(res, text);
      if (res.status === 401) {
        throw new McpError('MCP_UNAUTHORIZED',
          'MCP 密钥无效或已被重置，请让管理员重新下发本站点 MCP 密钥。',
          { httpStatus: 401, detail: info.detail });
      }
      if (res.status === 403) {
        throw new McpError('MCP_FORBIDDEN',
          '本站点没有调用该 MCP 工具的权限。',
          { httpStatus: 403, detail: info.detail });
      }
      if (res.status === 404 || res.status === 405) {
        throw new McpError('MCP_DISABLED',
          '站点 MCP 未启用或地址不正确。',
          { httpStatus: res.status, detail: info.detail });
      }
      throw new McpError('MCP_UPSTREAM', 'MCP 服务返回异常。', { httpStatus: res.status, detail: info.detail });
    }

    // 通知类请求：202 / 空体是正常的
    if (payload.id == null) return null;
    if (!text) return null;

    const ct = (res.headers.get('content-type') || '').toLowerCase();
    const body = ct.includes('text/event-stream') ? pickFromSse(text, payload.id) : safeJson(text);
    if (!body) {
      // 有些实现 SSE 但 content-type 没写对，兜底再试一次
      const fallback = pickFromSse(text, payload.id);
      if (fallback) return fallback;
      throw new McpError('MCP_BAD_RESPONSE', 'MCP 返回内容无法解析。', { detail: text.slice(0, 200) });
    }
    if (body.error) {
      throw new McpError('MCP_RPC_ERROR',
        body.error.message || '工具调用失败。',
        { detail: `${body.error.code ?? ''} ${body.error.message ?? ''}`.trim() });
    }
    return body;
  }

  async #initialize() {
    const id = this.nextId++;
    const res = await this.#post({
      jsonrpc: '2.0',
      id,
      method: 'initialize',
      params: {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: 'logistics-dashboard', version: '1.0.0' },
      },
    });
    this.serverInfo = (res && res.result && res.result.serverInfo) || null;

    // 必须发的通知，无 id
    await this.#post({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
    this.log.info(`mcp: 已初始化 server=${this.serverInfo ? JSON.stringify(this.serverInfo) : 'unknown'}`);
  }

  async listTools({ maxAgeMs = 5 * 60 * 1000, force = false } = {}) {
    await this.ensureReady();
    if (!force && this.toolsCache && Date.now() - this.toolsCachedAt < maxAgeMs) {
      return this.toolsCache;
    }
    const id = this.nextId++;
    const res = await this.#post({ jsonrpc: '2.0', id, method: 'tools/list', params: {} });
    const tools = (res && res.result && res.result.tools) || [];
    this.toolsCache = tools;
    this.toolsCachedAt = Date.now();
    return tools;
  }

  async callTool(name, args = {}) {
    await this.ensureReady();
    const id = this.nextId++;
    const res = await this.#post({
      jsonrpc: '2.0',
      id,
      method: 'tools/call',
      params: { name, arguments: args },
    });
    return (res && res.result) || null;
  }
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** 把 MCP 的工具结果压平成纯文本，方便直接回给前端展示 */
function flattenToolResult(result) {
  if (!result) return '';
  if (Array.isArray(result.content)) {
    return result.content
      .map((c) => {
        if (!c) return '';
        if (c.type === 'text') return c.text || '';
        if (c.type === 'json') return JSON.stringify(c.json);
        if (c.type === 'resource') return c.resource && c.resource.text ? c.resource.text : '';
        return `[${c.type || 'unknown'}]`;
      })
      .filter(Boolean)
      .join('\n');
  }
  if (typeof result === 'string') return result;
  return JSON.stringify(result);
}

module.exports = { McpClient, McpError, flattenToolResult };
