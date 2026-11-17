#!/usr/bin/env node
/**
 * 本地预览：用占位配置起服务，只为了看页面长什么样。
 * 真实登录会失败（连不上中枢），但登录页、错误页、助手页布局都能看。
 *
 * 用法：node test/preview.mjs
 */
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const port = process.env.PREVIEW_PORT || '18799';

const env = {
  ...process.env,
  OPS_SITE_ORIGIN: `http://127.0.0.1:${port}`,
  PORT: port,
  HOST: '127.0.0.1',
  TRUST_PROXY: 'false',
  OPS_CLIENT_ID: 'app_preview',
  OPS_CLIENT_SECRET: 'preview-secret',
  OPS_REDIRECT_URI: `http://127.0.0.1:${port}/ops/auth/callback`,
  OPS_AUTHORIZE_URL: 'https://ops.htwig.com:10000/oauth/authorize',
  OPS_TOKEN_URL: 'https://ops.htwig.com:10000/api/oauth/token',
  OPS_USERINFO_URL: 'https://ops.htwig.com:10000/api/oauth/userinfo',
  OPS_SITE_MCP_URL: '',
  OPS_SITE_MCP_KEY: '',
  OPS_SITE_AI_URL: '',
  OPS_SITE_AI_KEY: '',
  OPS_SITE_SKILLS_URL: '',
  COOKIE_SECURE: 'false',
};

const child = spawn(process.execPath, [path.join(ROOT, 'server', 'index.js')], {
  env,
  stdio: 'inherit',
});

console.log(`\n预览地址：http://127.0.0.1:${port}/`);
console.log('  /            登录引导页（未登录）');
console.log('  /assistant.html  AI 助手 / MCP / 技能');
console.log('Ctrl+C 结束\n');

process.on('SIGINT', () => { child.kill(); process.exit(0); });
child.on('exit', (c) => process.exit(c ?? 0));
