'use strict';
/**
 * 配置与环境变量。
 *
 * 所有密钥只在这里读、只在服务端用，绝不下发到浏览器、绝不写日志。
 * 缺关键项时直接拒绝启动，不进入"半配置"状态。
 */

const REQUIRED = [
  'OPS_SITE_ORIGIN',
  'OPS_CLIENT_ID',
  'OPS_REDIRECT_URI',
  'OPS_AUTHORIZE_URL',
  'OPS_TOKEN_URL',
  'OPS_USERINFO_URL',
];

const SECRETS = ['OPS_CLIENT_SECRET', 'OPS_SITE_MCP_KEY', 'OPS_SITE_AI_KEY'];

function bool(v, dflt) {
  if (v == null || v === '') return dflt;
  return /^(1|true|yes|on)$/i.test(String(v));
}

function int(v, dflt) {
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) ? n : dflt;
}

function load(env = process.env) {
  const missing = REQUIRED.filter((k) => !env[k]);
  if (missing.length) {
    const msg = `缺少必需的环境变量: ${missing.join(', ')}`;
    const err = new Error(msg);
    err.code = 'CONFIG_MISSING';
    err.missing = missing;
    throw err;
  }

  const origin = env.OPS_SITE_ORIGIN.replace(/\/+$/, '');
  const redirectUri = env.OPS_REDIRECT_URI;

  // redirect_uri 必须与登记的完全一致，这里做一次形状校验，避免上线才发现
  try {
    const u = new URL(redirectUri);
    if (!/^https?:$/.test(u.protocol)) throw new Error('协议必须是 http/https');
    if (bool(env.COOKIE_SECURE, u.protocol === 'https:') === true && u.protocol !== 'https:') {
      throw new Error('配了 Secure Cookie 但 redirect_uri 不是 https');
    }
  } catch (e) {
    const err = new Error(`OPS_REDIRECT_URI 不合法: ${e.message}`);
    err.code = 'CONFIG_BAD_REDIRECT';
    throw err;
  }

  const cfg = {
    // 站点自身
    origin,
    port: int(env.PORT, 18700),
    host: env.HOST || '0.0.0.0',
    // 是否在反代后面（决定是否信 X-Forwarded-Proto）
    trustProxy: bool(env.TRUST_PROXY, true),

    // OAuth
    clientId: env.OPS_CLIENT_ID,
    clientSecret: env.OPS_CLIENT_SECRET || '',
    redirectUri,
    authorizeUrl: env.OPS_AUTHORIZE_URL,
    tokenUrl: env.OPS_TOKEN_URL,
    userinfoUrl: env.OPS_USERINFO_URL,
    scope: env.OPS_SCOPE || 'openid profile',
    callbackPath: new URL(redirectUri).pathname,

    // MCP / AI / 技能
    mcpUrl: env.OPS_SITE_MCP_URL || '',
    mcpKey: env.OPS_SITE_MCP_KEY || '',
    aiUrl: (env.OPS_SITE_AI_URL || '').replace(/\/+$/, ''),
    aiKey: env.OPS_SITE_AI_KEY || '',
    skillsUrl: env.OPS_SITE_SKILLS_URL || '',

    // 会话与安全
    sessionTtlSec: int(env.SESSION_TTL_SEC, 8 * 3600),
    stateTtlSec: int(env.STATE_TTL_SEC, 600),
    cookieSecure: bool(env.COOKIE_SECURE, new URL(redirectUri).protocol === 'https:'),
    cookieSameSite: (env.COOKIE_SAME_SITE || 'Lax'),

    // 额度不足时的引导地址（没配就只显示文字）
    quotaRequestUrl: env.OPS_QUOTA_REQUEST_URL || '',
  };

  // 能力开关：缺哪个就关哪个，其余功能不受影响
  cfg.features = {
    mcp: Boolean(cfg.mcpUrl && cfg.mcpKey),
    ai: Boolean(cfg.aiUrl && cfg.aiKey),
    skills: Boolean(cfg.skillsUrl && cfg.aiKey),
  };

  return cfg;
}

/** 启动自检摘要：只报"有/没有"，绝不打印值 */
function summarize(cfg) {
  const lines = [
    `站点 origin      : ${cfg.origin}`,
    `监听             : ${cfg.host}:${cfg.port}  (trustProxy=${cfg.trustProxy})`,
    `回调路径         : ${cfg.callbackPath}`,
    `client_id        : ${cfg.clientId}`,
    `client_secret    : ${cfg.clientSecret ? '已配置' : '未配置'}`,
    `scope            : ${cfg.scope}`,
    `Cookie Secure    : ${cfg.cookieSecure}  SameSite=${cfg.cookieSameSite}`,
    `MCP              : ${cfg.features.mcp ? '启用' : '未配置（跳过）'}`,
    `AI               : ${cfg.features.ai ? '启用' : '未配置（跳过）'}`,
    `技能             : ${cfg.features.skills ? '启用' : '未配置（跳过）'}`,
  ];
  // 兜底断言：任何密钥值都不能出现在摘要里
  for (const k of SECRETS) {
    const v = cfg[k.replace('OPS_CLIENT_SECRET', 'clientSecret')
      .replace('OPS_SITE_MCP_KEY', 'mcpKey')
      .replace('OPS_SITE_AI_KEY', 'aiKey')];
    if (v && v.length > 3 && lines.join('\n').includes(v)) {
      throw new Error('自检摘要里出现了密钥值，已中止启动');
    }
  }
  return lines.join('\n');
}

module.exports = { load, summarize, SECRETS };
