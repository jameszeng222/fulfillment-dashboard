'use strict';
/**
 * 站点 AI 接入（需求 6）。
 *
 * 用 OPS_SITE_AI_URL + OPS_SITE_AI_KEY，额度由应用独立承担。
 * 先 GET /models 拿可用模型，再按模型协议调 /chat/completions、/responses 或 /messages。
 * 额度不足时回一个稳定的错误码，由前端展示申请入口。
 */

const { timeoutFetch, describeUpstream } = require('./util');

class AiError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'AiError';
    this.code = code;
    this.httpStatus = extra.httpStatus || 502;
    this.detail = extra.detail || '';
    this.justification = extra.justification || '';
  }
}

/** 从上游错误里判断是不是"额度不足"，各家措辞不同，按关键词兜底 */
function looksLikeQuota(status, detail) {
  if (status === 402) return true;
  const d = String(detail || '').toLowerCase();
  return /quota|额度|配额|余额|不足|exceeded|out of credit|insufficient/.test(d);
}

class AiClient {
  constructor({ baseUrl, key, log, defaultProtocol = 'chat' }) {
    this.baseUrl = baseUrl;
    this.key = key;
    this.log = log;
    this.defaultProtocol = defaultProtocol;
    this.models = null;
    this.modelsAt = 0;
  }

  async #req(path, init) {
    const res = await timeoutFetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.key}`,
        Accept: 'application/json',
        ...(init && init.headers),
      },
    }, 60000);
    const text = await res.text();
    return { res, text };
  }

  async listModels({ maxAgeMs = 10 * 60 * 1000, force = false } = {}) {
    if (!force && this.models && Date.now() - this.modelsAt < maxAgeMs) return this.models;
    const { res, text } = await this.#req('/models', { method: 'GET' });
    if (!res.ok) {
      const info = describeUpstream(res, text);
      if (looksLikeQuota(res.status, info.detail)) {
        throw new AiError('AI_QUOTA', 'AI 额度不足。', { httpStatus: res.status, detail: info.detail });
      }
      if (res.status === 401 || res.status === 403) {
        throw new AiError('AI_UNAUTHORIZED', '站点 AI Key 无效或未授权。', { httpStatus: res.status, detail: info.detail });
      }
      throw new AiError('AI_UPSTREAM', '获取模型列表失败。', { httpStatus: res.status, detail: info.detail });
    }
    let json = {};
    try {
      json = JSON.parse(text);
    } catch {
      throw new AiError('AI_BAD_RESPONSE', '模型列表不是 JSON。', { detail: text.slice(0, 200) });
    }
    const raw = Array.isArray(json) ? json : (json.data || json.models || []);
    this.models = raw.map((m) => {
      if (typeof m === 'string') return { id: m };
      return {
        id: m.id || m.name || m.model || '',
        // 若平台给出了协议提示就采用，没有则走默认
        protocol: m.protocol || m.api_style || m.type || '',
        ownedBy: m.owned_by || '',
      };
    }).filter((m) => m.id);
    this.modelsAt = Date.now();
    return this.models;
  }

  /** 按模型挑协议：优先用模型自带的 protocol，其次看命名习惯，最后用默认值 */
  resolveProtocol(model) {
    const explicit = (model && model.protocol || '').toLowerCase();
    if (explicit.includes('response')) return 'responses';
    if (explicit.includes('message') || explicit.includes('anthropic')) return 'messages';
    if (explicit.includes('chat')) return 'chat';
    const id = String(model && model.id || '').toLowerCase();
    if (id.startsWith('claude')) return 'messages';
    if (this.defaultProtocol !== 'chat') return this.defaultProtocol;
    return 'chat';
  }

  async chat({ model, messages, maxTokens = 1200, signal }) {
    const models = await this.listModels();
    const chosen = model ? models.find((m) => m.id === model) || { id: model } : models[0];
    if (!chosen || !chosen.id) throw new AiError('AI_NO_MODEL', '没有可用模型。');

    const protocol = this.resolveProtocol(chosen);
    let path;
    let body;
    if (protocol === 'messages') {
      path = '/messages';
      body = {
        model: chosen.id,
        max_tokens: maxTokens,
        messages: messages.map((m) => ({ role: m.role, content: m.content })),
      };
    } else if (protocol === 'responses') {
      path = '/responses';
      body = {
        model: chosen.id,
        max_output_tokens: maxTokens,
        input: messages.map((m) => ({ role: m.role, content: m.content })),
      };
    } else {
      path = '/chat/completions';
      body = { model: chosen.id, max_tokens: maxTokens, messages, stream: false };
    }

    const { res, text } = await this.#req(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const info = describeUpstream(res, text);
      if (looksLikeQuota(res.status, info.detail)) {
        throw new AiError('AI_QUOTA', 'AI 额度不足，请申请增加额度。', { httpStatus: res.status, detail: info.detail });
      }
      throw new AiError('AI_UPSTREAM', 'AI 调用失败。', { httpStatus: res.status, detail: info.detail });
    }

    let json;
    try {
      json = JSON.parse(text);
    } catch {
      throw new AiError('AI_BAD_RESPONSE', 'AI 返回内容不是 JSON。', { detail: text.slice(0, 200) });
    }
    return { text: extractText(json, protocol), model: chosen.id, protocol, raw: json };
  }
}

/** 三种协议的回包结构不同，统一抽出纯文本 */
function extractText(json, protocol) {
  if (!json || typeof json !== 'object') return '';
  if (protocol === 'messages') {
    const parts = json.content || [];
    return parts.filter((p) => p && (p.type === 'text' || p.text)).map((p) => p.text || '').join('').trim();
  }
  if (protocol === 'responses') {
    if (typeof json.output_text === 'string') return json.output_text.trim();
    const items = json.output || [];
    return items.flatMap((i) => i && i.content ? i.content : [])
      .filter((c) => c && (c.text || c.output_text))
      .map((c) => c.text || c.output_text).join('').trim();
  }
  const choice = (json.choices || [])[0];
  return (choice && choice.message && choice.message.content || '').trim();
}

module.exports = { AiClient, AiError, extractText, looksLikeQuota };
