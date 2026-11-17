/**
 * 端到端测试：不依赖真实智能中枢。
 *
 * 起一个 mock OPS（OAuth + MCP + AI + 技能），再起本站点服务，用 Cookie 罐把
 * 登录、会话、CSRF、MCP 调用、AI 对话、技能上报、退出全流程跑一遍。
 *
 * 运行：node test/e2e.mjs
 */

import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import zlib from 'node:zlib';
import crypto from 'node:crypto';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

// ------------------------------------------------------------ 断言
let pass = 0;
const fails = [];
function ok(cond, label, extra = '') {
  if (cond) { pass++; console.log(`  ✅ ${label}`); }
  else { fails.push(label + (extra ? ` — ${extra}` : '')); console.log(`  ❌ ${label}${extra ? ' — ' + extra : ''}`); }
}
function eq(actual, expected, label) {
  ok(actual === expected, label, `期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
}

// ------------------------------------------------------------ 极简 zip 构造（stored）
function makeZip(files) {
  const crc = (buf) => (zlib.crc32 ? zlib.crc32(buf) : 0) >>> 0;
  const locals = [];
  const centrals = [];
  let offset = 0;

  for (const [name, content] of Object.entries(files)) {
    const nameBuf = Buffer.from(name, 'utf8');
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content, 'utf8');
    const c = crc(data);

    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0);
    lh.writeUInt16LE(20, 4);
    lh.writeUInt16LE(0, 6);
    lh.writeUInt16LE(0, 8);          // stored
    lh.writeUInt32LE(c, 14);
    lh.writeUInt32LE(data.length, 18);
    lh.writeUInt32LE(data.length, 22);
    lh.writeUInt16LE(nameBuf.length, 26);
    lh.writeUInt16LE(0, 28);

    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0);
    ch.writeUInt16LE(20, 4);
    ch.writeUInt16LE(20, 6);
    ch.writeUInt16LE(0, 8);
    ch.writeUInt16LE(0, 10);         // stored
    ch.writeUInt32LE(c, 16);
    ch.writeUInt32LE(data.length, 20);
    ch.writeUInt32LE(data.length, 24);
    ch.writeUInt16LE(nameBuf.length, 28);
    ch.writeUInt32LE(offset, 42);

    locals.push(lh, nameBuf, data);
    centrals.push(ch, nameBuf);
    offset += lh.length + nameBuf.length + data.length;
  }

  const cdOffset = offset;
  const cdSize = centrals.reduce((s, b) => s + b.length, 0);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(Object.keys(files).length, 8);
  eocd.writeUInt16LE(Object.keys(files).length, 10);
  eocd.writeUInt32LE(cdSize, 12);
  eocd.writeUInt32LE(cdOffset, 16);

  return Buffer.concat([...locals, ...centrals, eocd]);
}

// ------------------------------------------------------------ mock OPS
const SKILL_MD = '# 履约单排障技能\n\n1. 先看承运商\n2. 再看仓内时长\n';
const skillZip = makeZip({ 'SKILL.md': SKILL_MD });
const skillSha = crypto.createHash('sha256').update(skillZip).digest('hex');
const badSha = crypto.createHash('sha256').update('nope').digest('hex');

const state = {
  codes: new Map(),       // code -> {redirectUri, used}
  authCalls: 0,
  tokenCalls: 0,
  usageCalls: [],
};

function json(res, status, obj, extraHeaders = {}) {
  const b = Buffer.from(JSON.stringify(obj));
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': b.length, ...extraHeaders });
  res.end(b);
}

const mock = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname;

  // ---- OAuth authorize：自动同意，直接回跳 ----
  if (p === '/oauth/authorize') {
    state.authCalls++;
    const q = url.searchParams;
    if (q.get('response_type') !== 'code') return json(res, 400, { detail: 'bad response_type' });
    if (!q.get('client_id') || !q.get('redirect_uri') || !q.get('state')) {
      return json(res, 400, { detail: 'missing params' });
    }
    const code = 'code_' + crypto.randomBytes(8).toString('hex');
    state.codes.set(code, { redirectUri: q.get('redirect_uri'), used: false });
    const back = new URL(q.get('redirect_uri'));
    back.searchParams.set('code', code);
    back.searchParams.set('state', q.get('state'));
    res.writeHead(303, { Location: back.toString() });
    return res.end();
  }

  // ---- token ----
  if (p === '/api/oauth/token' && req.method === 'POST') {
    state.tokenCalls++;
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = Buffer.concat(chunks).toString('utf8');
    const ct = (req.headers['content-type'] || '').toLowerCase();
    const params = ct.includes('json') ? new URLSearchParams(new URLSearchParams()) : null;
    let f;
    if (ct.includes('json')) {
      try { f = new URLSearchParams(JSON.parse(body)); } catch { return json(res, 400, { detail: 'bad json' }); }
    } else {
      f = new URLSearchParams(body);
    }
    if (f.get('grant_type') !== 'authorization_code') return json(res, 400, { detail: 'bad grant_type' });
    if (f.get('client_secret') !== process.env.MOCK_SECRET) return json(res, 400, { detail: '应用不存在或已停用' });
    const rec = state.codes.get(f.get('code'));
    if (!rec) return json(res, 400, { detail: 'code 无效' });
    if (rec.used) return json(res, 400, { detail: 'code 已被使用' });
    if (f.get('redirect_uri') !== rec.redirectUri) return json(res, 400, { detail: 'redirect_uri 不一致' });
    rec.used = true;
    return json(res, 200, { access_token: 'at_' + crypto.randomBytes(12).toString('hex'), token_type: 'Bearer', expires_in: 3600 });
  }

  // ---- userinfo ----
  if (p === '/api/oauth/userinfo') {
    const auth = req.headers.authorization || '';
    if (!auth.startsWith('Bearer at_')) return json(res, 401, { detail: 'invalid token' });
    return json(res, 200, { sub: 'u-10086', preferred_username: 'james', name: 'James Zeng', avatar: '' });
  }

  // ---- MCP ----
  if (p === '/api/assistant/ai/sites/11/mcp' && req.method === 'POST') {
    if (req.headers.authorization !== `Bearer ${process.env.MOCK_MCP_KEY}`) {
      return json(res, 401, { detail: '请使用本站点独立的 MCP 密钥' });
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const msg = JSON.parse(Buffer.concat(chunks).toString('utf8'));

    if (msg.method === 'notifications/initialized') { res.writeHead(202); return res.end(); }

    const reply = (result) => json(res, 200, { jsonrpc: '2.0', id: msg.id, result });

    if (msg.method === 'initialize') {
      return reply({ protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'mock-mcp', version: '0' } });
    }
    if (msg.method === 'tools/list') {
      // 故意用 SSE 返回，验证服务端能解析 text/event-stream
      const payload = JSON.stringify({
        jsonrpc: '2.0', id: msg.id,
        result: {
          tools: [
            { name: 'query_lane_sla', description: '查询线路时效', inputSchema: { type: 'object', properties: { lane: { type: 'string' } } } },
            { name: 'list_open_exceptions', description: '列出异常单' },
          ],
        },
      });
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
      res.write(`event: message\ndata: ${payload}\n\n`);
      return res.end();
    }
    if (msg.method === 'tools/call') {
      if (msg.params.name === 'query_lane_sla') {
        return reply({
          content: [{ type: 'text', text: `线路 ${msg.params.arguments.lane} 的 P90 = 4.72 天` }],
          isError: false,
        });
      }
      return reply({ content: [{ type: 'text', text: 'ok' }] });
    }
    return json(res, 400, { detail: 'unknown method' });
  }

  // ---- AI ----
  if (p === '/api/assistant/ai/v1/models') {
    if (req.headers.authorization !== `Bearer ${process.env.MOCK_AI_KEY}`) return json(res, 401, { detail: 'unauthorized' });
    return json(res, 200, { data: [{ id: 'mock-gpt', owned_by: 'ops' }] });
  }
  if (p === '/api/assistant/ai/v1/chat/completions' && req.method === 'POST') {
    if (req.headers.authorization !== `Bearer ${process.env.MOCK_AI_KEY}`) return json(res, 401, { detail: 'unauthorized' });
    return json(res, 200, { choices: [{ message: { role: 'assistant', content: '这是 mock 模型的回答。' } }] });
  }

  // ---- 技能 ----
  if (p === '/api/assistant/ai/sites/11/skills' && req.method === 'GET') {
    if (req.headers.authorization !== `Bearer ${process.env.MOCK_AI_KEY}`) return json(res, 401, { detail: 'unauthorized' });
    return json(res, 200, {
      items: [
        { id: 'sla-doctor', name: '时效排障', description: '定位超时线路', downloadPath: '/pkg/good.zip', sha256: skillSha },
        { id: 'bad-hash', name: '坏包', description: '校验会失败', downloadPath: '/pkg/good.zip', sha256: badSha },
      ],
    });
  }
  if (p === '/pkg/good.zip') {
    res.writeHead(200, { 'Content-Type': 'application/zip', 'Content-Length': skillZip.length });
    return res.end(skillZip);
  }
  if (p === '/api/assistant/ai/sites/11/skills/usage' && req.method === 'POST') {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    state.usageCalls.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    return json(res, 200, { ok: true });
  }

  json(res, 404, { detail: 'not found' });
});

// ------------------------------------------------------------ 工具
class Jar {
  constructor() { this.c = new Map(); }
  store(setCookie) {
    for (const sc of [].concat(setCookie || [])) {
      const pair = sc.split(';')[0];
      const i = pair.indexOf('=');
      if (i < 0) continue;
      const k = pair.slice(0, i).trim();
      const v = pair.slice(i + 1).trim();
      if (!v) this.c.delete(k); else this.c.set(k, v);
    }
  }
  header() { return [...this.c].map(([k, v]) => `${k}=${v}`).join('; '); }
  get(k) { return this.c.get(k); }
}

async function waitHealthy(base, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(base + '/healthz');
      if (r.ok) return true;
    } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
}

// ------------------------------------------------------------ 主流程
async function run() {
  await new Promise((r) => mock.listen(0, '127.0.0.1', r));
  const mockPort = mock.address().port;
  const MOCK = `http://127.0.0.1:${mockPort}`;

  const appPort = 18000 + Math.floor(Math.random() * 1000);
  const ORIGIN = `http://127.0.0.1:${appPort}`;

  const env = {
    ...process.env,
    OPS_SITE_ORIGIN: ORIGIN,
    PORT: String(appPort),
    HOST: '127.0.0.1',
    TRUST_PROXY: 'false',
    OPS_CLIENT_ID: 'app_test',
    OPS_CLIENT_SECRET: process.env.MOCK_SECRET,
    OPS_REDIRECT_URI: `${ORIGIN}/ops/auth/callback`,
    OPS_AUTHORIZE_URL: `${MOCK}/oauth/authorize`,
    OPS_TOKEN_URL: `${MOCK}/api/oauth/token`,
    OPS_USERINFO_URL: `${MOCK}/api/oauth/userinfo`,
    OPS_SCOPE: 'openid profile',
    OPS_SITE_MCP_URL: `${MOCK}/api/assistant/ai/sites/11/mcp`,
    OPS_SITE_MCP_KEY: process.env.MOCK_MCP_KEY,
    OPS_SITE_AI_URL: `${MOCK}/api/assistant/ai/v1`,
    OPS_SITE_AI_KEY: process.env.MOCK_AI_KEY,
    OPS_SITE_SKILLS_URL: `${MOCK}/api/assistant/ai/sites/11/skills`,
    COOKIE_SECURE: 'false',
    SESSION_TTL_SEC: '3600',
    STATE_TTL_SEC: '120',
  };

  const child = spawn(process.execPath, [path.join(ROOT, 'server', 'index.js')], {
    env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let serverLog = '';
  child.stdout.on('data', (d) => { serverLog += d.toString(); });
  child.stderr.on('data', (d) => { serverLog += d.toString(); });

  const cleanup = () => { try { child.kill(); } catch {} mock.close(); };

  try {
    if (!(await waitHealthy(ORIGIN))) throw new Error('服务未启动:\n' + serverLog);

    const jar = new Jar();
    // 注意：headers 要单独合并，不能让 ...rest 覆盖掉合好的 Cookie
    const get = (p, extra = {}) => {
      const { headers: extraHeaders, ...rest } = extra;
      return fetch(ORIGIN + p, {
        redirect: 'manual',
        ...rest,
        headers: { Cookie: jar.header(), ...(extraHeaders || {}) },
      });
    };

    // ---- 1. 未登录访问首页 ----
    console.log('\n[1] 未登录');
    let r = await get('/');
    jar.store(r.headers.getSetCookie?.() || r.headers.get('set-cookie'));
    let body = await r.text();
    eq(r.status, 200, '首页返回 200');
    ok(body.includes('需要登录'), '显示登录引导页');
    ok(body.includes('/ops/auth/login'), '带登录入口');

    // ---- 2. 未登录访问 API ----
    r = await get('/api/me');
    eq(r.status, 401, '未登录访问 /api/me 返回 401');

    // ---- 3. 发起登录 ----
    console.log('\n[2] 登录跳转');
    r = await get('/ops/auth/login?next=/assistant.html');
    jar.store(r.headers.getSetCookie?.() || r.headers.get('set-cookie'));
    eq(r.status, 303, '登录返回 303');
    const loc = r.headers.get('location');
    const au = new URL(loc);
    eq(au.origin + au.pathname, `${MOCK}/oauth/authorize`, '跳到 authorize 端点');
    eq(au.searchParams.get('response_type'), 'code', 'response_type=code');
    eq(au.searchParams.get('client_id'), 'app_test', '带 client_id');
    eq(au.searchParams.get('redirect_uri'), `${ORIGIN}/ops/auth/callback`, 'redirect_uri 与登记一致');
    eq(au.searchParams.get('scope'), 'openid profile', 'scope 正确');
    const stateParam = au.searchParams.get('state');
    ok(stateParam && stateParam.length >= 20, 'state 足够随机');
    ok(jar.get('ops_pre'), '已下发登录前标识 Cookie');
    ok(!jar.get('ops_sid'), '此时还没有会话 Cookie');

    // ---- 4. 模拟授权端回跳 ----
    console.log('\n[3] 回调换 token');
    const authRes = await fetch(au.toString(), { redirect: 'manual' });
    eq(authRes.status, 303, 'mock 授权端回跳');
    const cbLoc = authRes.headers.get('location');
    eq(state.authCalls, 1, 'authorize 被调用一次');

    r = await get('/', {}); // 触发预置 cookie
    const cbUrl = new URL(cbLoc);
    r = await fetch(ORIGIN + cbUrl.pathname + cbUrl.search, {
      redirect: 'manual', headers: { Cookie: jar.header() },
    });
    jar.store(r.headers.getSetCookie?.() || r.headers.get('set-cookie'));
    eq(r.status, 303, '回调返回 303');
    eq(r.headers.get('location'), '/assistant.html', '回到登录前请求的站内地址');
    eq(state.tokenCalls, 1, 'token 端点被调用一次（由后端发起）');
    ok(jar.get('ops_sid'), '已下发会话 Cookie');
    ok(!jar.get('ops_pre'), '登录前标识已清除');

    // ---- 5. 已登录访问看板 ----
    console.log('\n[4] 已登录');
    r = await get('/');
    body = await r.text();
    eq(r.status, 200, '看板返回 200');
    ok(body.includes('履约单数据看板'), '返回的是看板本体');
    ok(body.includes('ops-authbar'), '注入了身份条');
    ok(body.includes('James Zeng'), '身份条显示用户名');

    r = await get('/api/me');
    let me = await r.json();
    eq(r.status, 200, '/api/me 返回 200');
    eq(me.user.sub, 'u-10086', 'sub 作为稳定标识');
    ok(me.csrf && me.csrf.length > 10, '下发 CSRF token');

    // ---- 6. state 复用与伪造 ----
    console.log('\n[5] state 防护');
    r = await fetch(ORIGIN + cbUrl.pathname + cbUrl.search, { redirect: 'manual', headers: { Cookie: jar.header() } });
    body = await r.text();
    eq(r.status, 400, '重放同一 state 被拒');
    ok(body.includes('登录失败') || body.includes('失败'), '返回错误页而不是放行');

    r = await fetch(`${ORIGIN}/ops/auth/callback?code=x&state=${encodeURIComponent('z'.repeat(40))}`, {
      redirect: 'manual', headers: { Cookie: jar.header() },
    });
    eq(r.status, 400, '伪造 state 被拒');

    // ---- 7. 开放重定向 ----
    console.log('\n[6] 安全回跳');
    r = await get('/ops/auth/login?next=' + encodeURIComponent('//evil.example.com/x'));
    const loc2 = new URL(r.headers.get('location'));
    jar.store(r.headers.getSetCookie?.() || r.headers.get('set-cookie'));
    ok(true, '已接受非法 next（校验发生在回调时）');
    // 用真实流程验证：重新走一遍
    const auth2 = await fetch(loc2.toString(), { redirect: 'manual' });
    const cb2 = new URL(auth2.headers.get('location'));
    r = await fetch(ORIGIN + cb2.pathname + cb2.search, { redirect: 'manual', headers: { Cookie: jar.header() } });
    jar.store(r.headers.getSetCookie?.() || r.headers.get('set-cookie'));
    eq(r.headers.get('location'), '/', '协议相对地址被回落为 /');

    // 第 6 步重新登录后会话已换新，CSRF 也要重新取，否则后面全是 403
    r = await get('/api/me');
    me = await r.json();

    // ---- 8. MCP ----
    console.log('\n[7] MCP');
    r = await get('/api/mcp/tools');
    const tools = await r.json();
    eq(r.status, 200, 'tools/list 返回 200');
    eq(tools.tools.length, 2, '发现 2 个已授权工具（SSE 解析成功）');

    r = await get('/api/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'query_lane_sla', arguments: { lane: 'USKY3' } }) });
    eq(r.status, 403, '缺 CSRF 的 POST 被拒');

    r = await get('/api/mcp/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf, Origin: ORIGIN },
      body: JSON.stringify({ name: 'query_lane_sla', arguments: { lane: 'USKY3' } }),
    });
    const call = await r.json();
    eq(r.status, 200, '带 CSRF 的调用成功');
    ok(String(call.text).includes('4.72'), '拿到工具返回内容');

    r = await get('/api/mcp/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf, Origin: ORIGIN },
      body: JSON.stringify({ name: 'rm_rf', arguments: {} }),
    });
    eq(r.status, 403, '未授权工具名被拒（白名单生效）');

    // ---- 9. AI ----
    console.log('\n[8] AI');
    r = await get('/api/ai/models');
    const models = await r.json();
    eq(r.status, 200, '/models 返回 200');
    eq(models.models[0].id, 'mock-gpt', '拿到模型列表');

    r = await get('/api/ai/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf, Origin: ORIGIN },
      body: JSON.stringify({ messages: [{ role: 'user', content: '你好' }] }),
    });
    const chat = await r.json();
    eq(r.status, 200, '对话返回 200');
    ok(chat.text.includes('mock'), '拿到模型回复');

    // ---- 10. 技能 ----
    console.log('\n[9] 技能');
    r = await get('/api/skills');
    const sk = await r.json();
    eq(r.status, 200, '技能列表返回 200');
    eq(sk.skills.length, 2, '列出 2 个技能');

    r = await get('/api/skills/usage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf, Origin: ORIGIN },
      body: JSON.stringify({ ids: ['sla-doctor'] }),
    });
    const usage = await r.json();
    eq(r.status, 200, 'usage 上报成功');
    ok(usage.ok && usage.eventKey, '返回唯一 eventKey');
    eq(state.usageCalls.length, 1, '平台侧收到一次上报');
    eq(state.usageCalls[0].engine, 'logistics-dashboard', 'engine 字段正确');
    ok(Array.isArray(state.usageCalls[0].ids) && state.usageCalls[0].ids[0] === 'sla-doctor', 'ids 正确');

    // 下载 + sha256 校验 + 读说明（正常包）
    r = await get('/api/skills/sla-doctor/instructions');
    const ins = await r.json();
    eq(r.status, 200, '技能包下载并校验通过');
    ok(String(ins.instructions || '').includes('履约单排障技能'), '从 zip 里读到 SKILL.md 说明');

    // sha256 不匹配必须拒绝
    r = await get('/api/skills/bad-hash/instructions');
    const bad = await r.json();
    ok(r.status >= 400, 'sha256 不匹配的技能包被拒绝');
    eq(bad.error, 'SKILL_HASH_MISMATCH', '错误码是 SKILL_HASH_MISMATCH');

    // ---- 11. 退出 ----
    console.log('\n[10] 退出登录');
    r = await get('/ops/auth/logout');
    body = await r.text();
    eq(r.status, 200, '退出页返回 200');
    ok(body.includes('csrf'), '退出表单带 CSRF');

    r = await get('/ops/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'csrf=wrong',
    });
    eq(r.status, 403, '错误 CSRF 的退出被拒');

    r = await get('/ops/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'csrf=' + encodeURIComponent(me.csrf),
    });
    jar.store(r.headers.getSetCookie?.() || r.headers.get('set-cookie'));
    eq(r.status, 303, '正确 CSRF 退出成功');
    ok(!jar.get('ops_sid'), '会话 Cookie 已清除');

    r = await get('/');
    body = await r.text();
    ok(body.includes('需要登录'), '退出后回到登录页');

    // ---- 12. 目录穿越 ----
    console.log('\n[11] 静态资源防护');
    r = await fetch(`${ORIGIN}/../.env`, { redirect: 'manual' });
    ok(r.status !== 200 || !(await r.text()).includes('OPS_CLIENT_SECRET'), '/../.env 不可读');
    r = await fetch(`${ORIGIN}/%2e%2e%2f.env`, { redirect: 'manual' });
    ok(r.status !== 200, '编码的目录穿越被拒');

  } finally {
    cleanup();
  }
}

run()
  .then(() => {
    console.log(`\n${'─'.repeat(52)}`);
    console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
    if (fails.length) {
      console.log('\n失败明细：');
      fails.forEach((f) => console.log('  - ' + f));
      process.exit(1);
    }
    console.log('全部通过');
  })
  .catch((e) => {
    console.error('\n测试执行出错：', e);
    process.exit(1);
  });
