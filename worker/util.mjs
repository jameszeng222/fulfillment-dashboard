/**
 * Worker 运行时工具：只用 Web 标准 API（无 node: 依赖）。
 * Base64URL / HMAC 签名 / Cookie / 恒定时间比较 / 带超时 fetch / 页面模板。
 */

const enc = new TextEncoder();
const dec = new TextDecoder();

export function b64urlEncode(bytes) {
  let s = '';
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(str) {
  const s = String(str).replace(/-/g, '+').replace(/_/g, '/');
  const pad = s.length % 4 ? '='.repeat(4 - (s.length % 4)) : '';
  const bin = atob(s + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** 恒定时间字符串比较，避免时序侧信道 */
export function safeEqual(a, b) {
  const x = String(a ?? '');
  const y = String(b ?? '');
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

export function randomToken(bytes = 32) {
  const u8 = new Uint8Array(bytes);
  crypto.getRandomValues(u8);
  return b64urlEncode(u8);
}

let _keyCache = new Map();
async function hmacKey(secret) {
  if (_keyCache.has(secret)) return _keyCache.get(secret);
  const k = await crypto.subtle.importKey(
    'raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  if (_keyCache.size > 8) _keyCache.clear();
  _keyCache.set(secret, k);
  return k;
}

export async function hmac(secret, data) {
  const k = await hmacKey(secret);
  const sig = await crypto.subtle.sign('HMAC', k, enc.encode(data));
  return b64urlEncode(new Uint8Array(sig));
}

/** 签名一个对象 → "payload.sig" */
export async function sign(secret, obj) {
  const p = b64urlEncode(enc.encode(JSON.stringify(obj)));
  return `${p}.${await hmac(secret, p)}`;
}

/** 校验并解出对象；签名不符或过期返回 null */
export async function unsign(secret, token, nowSec = Math.floor(Date.now() / 1000)) {
  if (typeof token !== 'string' || !token.includes('.')) return null;
  const i = token.lastIndexOf('.');
  const p = token.slice(0, i);
  const s = token.slice(i + 1);
  const expect = await hmac(secret, p);
  if (!safeEqual(s, expect)) return null;
  let obj;
  try {
    obj = JSON.parse(dec.decode(b64urlDecode(p)));
  } catch {
    return null;
  }
  if (!obj || typeof obj !== 'object') return null;
  if (typeof obj.exp === 'number' && obj.exp < nowSec) return null;
  return obj;
}

export function parseCookies(header) {
  const out = {};
  if (!header) return out;
  for (const part of String(header).split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    const k = part.slice(0, i).trim();
    const v = part.slice(i + 1).trim();
    if (!k) continue;
    try { out[k] = decodeURIComponent(v); } catch { out[k] = v; }
  }
  return out;
}

export function serializeCookie(name, value, opts = {}) {
  const bits = [`${name}=${encodeURIComponent(value)}`];
  bits.push(`Path=${opts.path || '/'}`);
  if (opts.maxAge != null) bits.push(`Max-Age=${Math.floor(opts.maxAge)}`);
  if (opts.httpOnly !== false) bits.push('HttpOnly');
  if (opts.secure) bits.push('Secure');
  bits.push(`SameSite=${opts.sameSite || 'Lax'}`);
  return bits.join('; ');
}

export function clearCookie(name, opts = {}) {
  return serializeCookie(name, '', { ...opts, maxAge: 0 });
}

/** 带超时的 fetch */
export async function timeoutFetch(url, init = {}, ms = 15000) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: ac.signal });
  } finally {
    clearTimeout(t);
  }
}

/**
 * 只允许站内相对路径，防止开放重定向。
 */
export function safeNext(raw, fallback = '/') {
  if (typeof raw !== 'string' || !raw.length || raw.length > 512) return fallback;
  if (!raw.startsWith('/')) return fallback;
  if (raw.startsWith('//')) return fallback;
  if (raw.includes('\\')) return fallback;
  if (/[\u0000-\u001f\u007f]/.test(raw)) return fallback;
  if (/^\/ops\/auth\//.test(raw)) return fallback;
  return raw;
}

/** 从上游错误体里抽出可公开的说明 */
export function describeUpstream(res, bodyText) {
  let detail = '';
  try {
    const j = JSON.parse(bodyText);
    detail = j.detail || j.error_description || j.error || j.message || '';
  } catch {
    detail = String(bodyText || '').slice(0, 200);
  }
  return { status: res.status, detail: String(detail).slice(0, 200) };
}

export const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

const BASE_CSS = `
:root{--bg:#f4f6fa;--card:#fff;--ink:#1b2430;--ink2:#5b6675;--line:#e6ebf2;--brand:#2F5597;--bad:#d9534f}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
     background:var(--bg);color:var(--ink);display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
.box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:32px 34px;
     max-width:520px;width:100%;box-shadow:0 6px 24px rgba(20,40,80,.07)}
h1{font-size:19px;font-weight:650;margin-bottom:10px}
p{font-size:13.5px;line-height:1.85;color:var(--ink2);margin-bottom:12px}
code{font-family:ui-monospace,Consolas,monospace;background:#f4f7fc;border:1px solid var(--line);
     border-radius:4px;padding:1px 5px;font-size:12.5px;color:#2F5597}
.btn{display:inline-block;margin-top:8px;background:var(--brand);color:#fff;text-decoration:none;
     border:0;border-radius:9px;padding:10px 20px;font-size:14px;font-family:inherit;cursor:pointer}
.err{color:var(--bad);font-weight:600}
.meta{margin-top:14px;font-size:12px;color:#9aa5b4;word-break:break-all}
`;

export function page(title, body) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><style>${BASE_CSS}</style></head>
<body><div class="box">${body}</div></body></html>`;
}

export function loginRequired(next = '/') {
  return page('需要登录', `
      <h1>需要登录</h1>
      <p>这个页面需要先通过智能中枢登录。</p>
      <a class="btn" href="/ops/auth/login?next=${encodeURIComponent(next)}">使用中枢账号登录</a>
      <div class="meta">登录后会回到 <code>${esc(next)}</code></div>`);
}

export function errorPage({ title = '登录失败', detail = '', code = '' }) {
  return page(title, `
      <h1><span class="err">${esc(title)}</span></h1>
      <p>${esc(detail)}</p>
      ${code ? `<div class="meta">错误码：<code>${esc(code)}</code></div>` : ''}
      <p style="margin-top:16px"><a class="btn" href="/ops/auth/login">重新登录</a></p>
      <div class="meta">如果反复失败，请联系管理员核对回调地址与凭据配置。</div>`);
}

export function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

export function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

export function redirectResponse(location, setCookies = []) {
  const h = new Headers({ Location: location, 'Cache-Control': 'no-store' });
  for (const c of setCookies) h.append('Set-Cookie', c);
  return new Response(null, { status: 303, headers: h });
}
