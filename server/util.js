'use strict';
/** 共用小工具：带超时的 fetch、脱敏日志、安全的重定向目标校验。 */

const SECRET_ENV = /^OPS_(CLIENT_SECRET|SITE_MCP_KEY|SITE_AI_KEY)$/;

/** 带超时的 fetch，避免上游挂住整个进程 */
async function timeoutFetch(url, init = {}, ms = 15000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(new Error('timeout')), ms);
  try {
    return await fetch(url, { ...init, signal: ac.signal });
  } finally {
    clearTimeout(timer);
  }
}

/** 结构化日志。自动把疑似密钥的值打码。 */
function makeLogger(prefix = '') {
  const secrets = Object.entries(process.env)
    .filter(([k, v]) => SECRET_ENV.test(k) && v && v.length >= 6)
    .map(([, v]) => v);

  const redact = (s) => {
    let out = String(s);
    for (const sec of secrets) out = out.split(sec).join('***');
    // 兜底：把常见 bearer / secret 形态也打掉
    out = out.replace(/(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, '$1***');
    return out;
  };

  const write = (level, args) => {
    const line = args
      .map((a) => (typeof a === 'string' ? a : JSON.stringify(a)))
      .join(' ');
    const stamp = new Date().toISOString();
    // eslint-disable-next-line no-console
    console[level === 'error' ? 'error' : 'log'](`${stamp} ${level} ${prefix}${redact(line)}`);
  };

  return {
    info: (...a) => write('info', a),
    warn: (...a) => write('warn', a),
    error: (...a) => write('error', a),
    redact,
  };
}

/**
 * 校验登录后跳转目标。只允许本站相对路径，禁止协议相对地址和反斜杠，
 * 防止开放重定向。非法一律回落到首页。
 */
function safeNext(raw, fallback = '/') {
  if (typeof raw !== 'string' || raw.length === 0 || raw.length > 512) return fallback;
  if (!raw.startsWith('/')) return fallback;      // 必须是绝对路径
  if (raw.startsWith('//')) return fallback;      // 协议相对，会跳到外站
  if (raw.includes('\\')) return fallback;        // 部分浏览器把 \ 当 /
  if (/[\u0000-\u001f\u007f]/.test(raw)) return fallback; // 控制字符
  if (/^\/ops\/auth\//.test(raw)) return fallback; // 别把自己跳回登录环
  return raw;
}

/** 把上游错误整理成可记录、但不含敏感信息的短描述 */
function describeUpstream(res, bodyText) {
  const truncated = (bodyText || '').slice(0, 300);
  let detail = '';
  try {
    const j = JSON.parse(bodyText);
    detail = j.detail || j.error_description || j.error || j.message || '';
  } catch {
    detail = truncated;
  }
  return { status: res.status, detail: String(detail).slice(0, 200) };
}

module.exports = { timeoutFetch, makeLogger, safeNext, describeUpstream };
