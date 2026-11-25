# 站点部署与验收（logistics-dashboard）

站点通过中枢平台发布到 `https://biz.htwig.com:18700`，启动入口是 `deploy/run_site.py`。

## 一、平台托管发布

1. 在会话中选择站点 `logistics-dashboard`，要求部署本项目。
2. 上传文件（保留相对路径）：

   - `app/`、`templates/`、`static/`、`public/`、`deploy/`
   - `requirements.txt`、`init_ops.py`、`alerts.py`、`track17.py`
   - `reference/`（平台对照表与承运商编码，可选）
   - 不需要上传：`test/`、`sql/`、`cloudflare/`、数据导入脚本、任何 `.env`、任何数据库或 Excel 文件

3. 入口填 `deploy/run_site.py`，运行方式选 `python3`，不要填 `direct`。

平台在启动入口注入 `OPS_CLIENT_ID`、`OPS_CLIENT_SECRET`、`OPS_REDIRECT_URI`、
`OPS_AUTHORIZE_URL`、`OPS_TOKEN_URL`、`OPS_USERINFO_URL`，站点 MCP 已启用时再注入
`OPS_SITE_MCP_URL`、`OPS_SITE_MCP_KEY`，并设置 `PORT`。应用只在后端读取这些变量。

启动脚本会：

- 首次运行创建 `.venv` 并按 `requirements.txt` 安装依赖（已有环境则直接复用）；
- 由 `OPS_REDIRECT_URI` 推导 `OPS_SITE_ORIGIN`，保证 Secure Cookie 与 `redirect_uri` 精确匹配；
- 补齐中枢服务地址（MCP / AI / 技能）与站点默认值；
- 首次运行用 `init_ops.py --no-seed` 建立空的操作库，不创建本地口令账号；
- 前台启动 `uvicorn` 监听 `0.0.0.0:$PORT`。

AI 与技能使用同一个应用 Key `OPS_SITE_AI_KEY`。平台未注入该 Key 时，助手会显示对应能力未配置，
登录、看板与 MCP 不受影响。

## 二、手工运行（本机或自有服务器）

```bash
cp .env.example .env        # 只填三个密钥，其余按站点填好
python3 deploy/run_site.py  # 默认监听 18700
python3 deploy/run_site.py --check   # 只打印配置摘要，不读密钥
```

密钥只从环境变量读取，脚本不会生成、写盘或打印密钥。

## 三、验收

| 检查 | 预期 |
|---|---|
| `GET /login` | 出现「使用智能中枢登录」按钮 |
| `GET /ops/auth/login` | 303 跳到 `OPS_AUTHORIZE_URL`，参数含 `response_type=code`、`client_id`、`redirect_uri`、`scope=openid profile`、随机 `state`；Cookie 为 `HttpOnly; Secure; SameSite=lax` |
| 完成中枢授权 | 跳回 `/ops/auth/callback`，再 303 到站内地址，地址不含 `code` / `state` |
| `GET /api/me` | 返回当前用户与 `csrf`；角色来自 `OPS_SIGNUP_ROLE` 或已绑定的站内账号 |
| `POST /api/logout` | 需带 `X-CSRF-Token`，成功后会话失效 |
| `/assistant.html` → MCP 工具 | 列出中枢授权给本站的工具；调用走 `tools/call` |
| `/assistant.html` → AI 对话 | 先 `GET /models`，再按协议调用；额度不足提示申请 |
| `/assistant.html` → 技能 | 列出授权技能，校验 `sha256` 后加载说明，使用后上报一次 |

未登录访问 `/api/me` 返回 401，伪造或重放 `state` 回到 `/login?error=state`。

## 四、业务数据

站点代码不包含业务数据。`履约单.duckdb`（事实层）与 `ops.db`（操作层）由运行环境保管：
`ops.db` 首次启动自动建空库；`履约单.duckdb` 需按既有数据流程在服务器上生成或导入，
缺失时页面接口会返回 500，登录与助手不受影响。数据文件不要打包进发布包。

## 五、排错

- 启动日志里 `[site]` 行为配置摘要，密钥只显示「已配置 / 未配置」。
- `OPS_CLIENT_ID`/`OPS_CLIENT_SECRET` 未配置时登录页提示「尚未配置」，说明平台注入未生效。
- `MCP_UNAUTHORIZED`：站点 MCP 密钥无效或已重置；`MCP_DISABLED`：站点未启用 MCP。
- `AI_QUOTA`：站点 AI 额度不足，前端展示申请入口（配置 `OPS_QUOTA_REQUEST_URL` 后给链接）。
