# 智能中枢（OPS）接入

把 OAuth 登录、站点 MCP、AI 对话、技能接到本站点。

> **前提**：看板本身是纯静态单文件，做不了服务端会话和密钥交换。
> 所以新增了一个 **Node 服务**（`server/`）来承担：登录、会话、后端代理。
> 看板的业务逻辑一行没改，只是被这个服务挡在登录之后。

---

## 一、跑起来

```bash
cp .env.example .env      # 填三个密钥（其余已按你的站点填好）
node server/index.js      # 默认监听 18700
```

三个密钥**只在服务端读**，来自环境变量，不进仓库、不进日志、不下发浏览器：

| 变量 | 说明 |
|---|---|
| `OPS_CLIENT_SECRET` | 换 token 用 |
| `OPS_SITE_MCP_KEY` | 本站点独立 MCP 密钥 |
| `OPS_SITE_AI_KEY` | 站点独立应用 Key（AI + 技能共用） |

任一能力没配就自动关闭对应入口，其余功能不受影响。

### 自检

```bash
node test/e2e.mjs         # 61 项断言，含 mock 中枢，不需要真实凭据
node test/preview.mjs     # 起一个只用于看页面的预览
```

`test/e2e.mjs` 内置了一个 mock OPS（OAuth + MCP + AI + 技能），把全流程跑通：
登录跳转、state 校验、换 token、取身份、会话、CSRF、MCP 工具发现与调用、
AI 对话、技能下载校验与上报、退出、目录穿越防护。

---

## 二、登录流程（对应需求 1~4）

```
浏览器                本站点后端                     智能中枢
  │  GET /              │                              │
  │ ──────────────────> │ 无会话 → 登录引导页           │
  │  GET /ops/auth/login│                              │
  │ ──────────────────> │ 生成随机 state(绑定浏览器)     │
  │ <── 303 authorize ──│                              │
  │ ──────────── 用户在中枢授权 ─────────────────────> │
  │ <──────── 303 /ops/auth/callback?code&state ───────│
  │  GET callback       │                              │
  │ ──────────────────> │ 1) 校验 error / state(单次)   │
  │                     │ 2) POST token(带 secret) ───> │
  │                     │ 3) GET userinfo(Bearer) ────> │
  │                     │ 4) 建会话, 下发 HttpOnly Cookie│
  │ <── 303 站内地址 ───│                              │
```

实现要点：

- **state**：32 字节随机，服务端存储，绑定登录前下发的 `ops_pre` Cookie，**单次使用**，默认 10 分钟过期。重放、伪造、跨浏览器都会被拒。
- **token 交换**：后端发起，表单提交，`redirect_uri` 与登录时及登记值三处一致。授权码 5 分钟单次有效。
- **身份**：`userinfo` 的 `sub` 作为稳定标识；只用 `preferred_username` / `name` / `avatar` 做展示。**不假设**有 id_token、refresh_token 或 OIDC discovery。
- **会话**：登录成功后发**全新的** `ops_sid`（防会话固定），服务端存，Cookie 为 `HttpOnly + SameSite=Lax + Secure`（HTTPS 下）。
- **CSRF**：会话里带一个 token，所有改变状态的请求必须带 `X-CSRF-Token`；同时校验 `Origin`。
- **回跳**：只接受站内相对路径，`//evil.com`、`\`、控制字符、`/ops/auth/*` 一律回落 `/`。
- **退出**：GET 给确认页（带 CSRF），POST 才真正销毁会话。

---

## 三、MCP（对应需求 5）

后端用 `Authorization: Bearer <OPS_SITE_MCP_KEY>` 请求 `OPS_SITE_MCP_URL`，
走 MCP Streamable HTTP / JSON-RPC 2.0：

```
initialize → notifications/initialized → tools/list → tools/call
```

- 请求头 `Accept: application/json, text/event-stream`，**两种响应都解析**（已用 SSE 测过）
- 兼容 `Mcp-Session-Id` 回传
- 前端只能调用 `tools/list` 里真实存在且已授权的工具，服务端有**白名单二次校验**
- 错误处理：401 → 密钥无效/被重置，403 → 权限不足，404/405 → 未启用

前端入口：`/assistant.html` → 「MCP 工具」页签。

---

## 四、AI 与技能（对应需求 6、7）

**AI**：`GET /models` 拿模型列表 → 按模型协议调 `/chat/completions`、`/responses` 或 `/messages`
（按 `protocol` 字段或模型名前缀判断，可用 `OPS_AI_PROTOCOL` 强制）。
额度不足（402 或含 quota/额度/配额等字样）时返回 `AI_QUOTA` 错误码，前端展示申请入口
（配了 `OPS_QUOTA_REQUEST_URL` 就给链接）。

**技能**：`GET <skills>` 拿列表 → 按 `downloadPath` 下载 → **sha256 必校验，不匹配直接丢弃** →
从 zip 里读 `SKILL.md` 作为技能说明（自带极简 zip 解析，不引 npm 包）→
使用后 `POST <skills>/usage` 上报 `{ids, eventKey, engine}`，`eventKey` 每次唯一。

前端入口：`/assistant.html` → 「AI 对话」「技能」页签。

---

## 五、部署到 biz.htwig.com:18700

这台机器从本地**不可达**（443/80/18700 三个端口 TCP 都超时），所以下面的步骤需要你在服务器上执行。

### 1. 放代码

```bash
git clone <repo> /opt/logistics-dashboard
cd /opt/logistics-dashboard
python3 assemble.py            # 生成 public/index.html
```

### 2. 配密钥

```bash
cp .env.example .env && vim .env     # 只填三个密钥
```

### 3. 常驻

```bash
npm init -y >/dev/null 2>&1   # 只为拿 pm2，本身无依赖
npx pm2 start server/index.js --name ops-dashboard
npx pm2 save
```

或者 systemd：

```ini
[Unit]
Description=Logistics Dashboard (OPS)
After=network.target

[Service]
WorkingDirectory=/opt/logistics-dashboard
EnvironmentFile=/opt/logistics-dashboard/.env
ExecStart=/usr/bin/node server/index.js
Restart=always

[Install]
WantedBy=multi-user.target
```

### 4. 反代（HTTPS 必须）

站点已经在 18700 上跑 HTTPS，如果前面有 nginx：

```nginx
location / {
  proxy_pass http://127.0.0.1:18700;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

**注意**：`TRUST_PROXY=true` 时服务端会看 `X-Forwarded-Proto`；`COOKIE_SECURE=true` 要求全程 HTTPS，
否则浏览器不会回传会话 Cookie——这是最常见的一类"登录成功但一直返回登录页"的原因。

### 5. 在中枢侧登记

回调地址必须与 `.env` 里的 `OPS_REDIRECT_URI` **完全一致**：
`https://biz.htwig.com:18700/ops/auth/callback`

---

## 六、还没验证的部分（需要你提供密钥后跑）

我用 mock 把逻辑全测了，但真实中枢的这几处只能联调确认：

| 项 | 现状 | 怎么确认 |
|---|---|---|
| `userinfo` 返回结构 | 按 `sub` 取，兼容 `{data:{...}}` | 登录一次，看 `/api/me` |
| MCP `tools/list` 真实工具名 | 只按列表动态展示 | 打开「MCP 工具」页签 |
| AI `/models` 与协议 | 支持三种，按字段判断 | 打开「AI 对话」页签 |
| 技能包内说明文件命名 | 认 `SKILL.md` / `README.md` / 任意 `.md` | 打开「技能」页签点加载 |
| 额度不足的错误形态 | 402 或含 quota/额度关键词 | 额度耗尽时观察 |

如果哪一项和我假设的不一样，把实际的响应结构发我，改起来很快——
所有解析都集中在 `server/ai.js`、`server/skills.js` 两个文件里。

---

## 七、安全清单

- [x] 三个密钥只在服务端读，日志自动打码（`server/util.js` 有兜底脱敏）
- [x] 会话 Cookie：`HttpOnly` + `SameSite=Lax` + HTTPS 下 `Secure`
- [x] 登录后换新 session id（防会话固定）
- [x] state 单次、限时、绑定浏览器
- [x] 所有状态变更校验 CSRF + Origin
- [x] 回跳只允许站内相对路径
- [x] MCP 工具名服务端白名单二次校验
- [x] 技能包 sha256 校验，不匹配丢弃
- [x] 静态资源目录穿越防护
- [x] 上游错误不把原文吐给浏览器（只给稳定错误码 + 中文提示）
- [ ] **站点是公网可访问的**：建议再加一层 IP 白名单或 Cloudflare Access，
      因为看板本身含真实业务数据（各平台单量、仓库、承运商分布）
