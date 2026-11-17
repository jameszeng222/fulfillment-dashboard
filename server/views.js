'use strict';
/** 极简 HTML 页面（登录引导、错误页、登出确认）。都不含外部资源。 */

const esc = (s) =>
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
.btn:hover{filter:brightness(1.08)}
.err{color:var(--bad);font-weight:600}
.meta{margin-top:14px;font-size:12px;color:#9aa5b4;word-break:break-all}
`;

function page({ title, body, status = 200 }) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><style>${BASE_CSS}</style></head>
<body><div class="box">${body}</div></body></html>`;
}

function loginRequired(next = '/') {
  const safeNext = esc(next);
  return page({
    title: '需要登录',
    body: `
      <h1>需要登录</h1>
      <p>这个页面需要先通过智能中枢登录。</p>
      <a class="btn" href="/ops/auth/login?next=${encodeURIComponent(safeNext)}">使用中枢账号登录</a>
      <div class="meta">登录后会回到 <code>${safeNext}</code></div>`,
  });
}

/**
 * 错误页。code 是给人看的短标识，detail 是可公开的说明。
 * 绝不把服务端响应原文（可能含密钥或内部信息）直接吐给浏览器。
 */
function errorPage({ status = 400, title = '登录失败', detail = '', code = '' }) {
  return page({
    status,
    title,
    body: `
      <h1><span class="err">${esc(title)}</span></h1>
      <p>${esc(detail)}</p>
      ${code ? `<div class="meta">错误码：<code>${esc(code)}</code></div>` : ''}
      <p style="margin-top:16px"><a class="btn" href="/ops/auth/login">重新登录</a></p>
      <div class="meta">如果反复失败，请联系系统管理员核对本站点的回调地址与凭据配置。</div>`,
  });
}

function logoutConfirm({ csrf }) {
  return page({
    title: '确认退出',
    body: `
      <h1>确认退出登录？</h1>
      <p>退出后需要重新通过智能中枢登录才能访问看板。</p>
      <form method="POST" action="/ops/auth/logout">
        <input type="hidden" name="csrf" value="${esc(csrf)}">
        <button class="btn" type="submit">退出登录</button>
      </form>
      <p style="margin-top:14px"><a href="/" style="font-size:13px;color:var(--ink2)">返回看板</a></p>`,
  });
}

module.exports = { page, loginRequired, errorPage, logoutConfirm, esc };
