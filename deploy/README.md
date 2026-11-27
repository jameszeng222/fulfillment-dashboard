# 中枢部署与验收（logistics-dashboard）

站点 11：`https://biz.htwig.com:18700`。中枢发布只使用 `ops/logistics-dashboard-hub` 已评审的版本。

1. 选择获授权的中枢站点，使用受管发布或 SSH 工具，启用站点绑定 TLS 证书。
2. 只发布 `app/`、`templates/`、`static/`、`public/`、`deploy/run_site.py`，以及 `requirements.txt`、`init_ops.py`、`alerts.py`、`track17.py` 和所需的 `reference/` 参考资源。不要上传数据目录、数据库、业务导出文件、日志、`.env`、凭据或本地虚拟环境。不要拿整仓覆盖运行目录；确认旧发布没有遗留的代理、通用透传或隧道配置。
3. 使用平台启动包装器运行 `python3 deploy/run_site.py`，由受管运行环境提供 `PORT`、`ICS_TLS_CERT_FILE` 和 `ICS_TLS_KEY_FILE`。应用在本站端口直接提供 HTTPS，不在 TLS 缺失时退回 HTTP。平台启动器重启时也应注入这两个 TLS 路径。关闭逐请求访问日志，避免记录订单标识与登录回调参数。

## 配置与数据

- 登录：`OPS_CLIENT_ID`、`OPS_CLIENT_SECRET`、`OPS_REDIRECT_URI`、`OPS_AUTHORIZE_URL`、`OPS_TOKEN_URL`、`OPS_USERINFO_URL`。回调为 `https://biz.htwig.com:18700/ops/auth/callback`；如配置 `OPS_SITE_ORIGIN`，须与回调的 HTTPS 源一致。
- 可选助手：`OPS_SITE_AI_URL`、`OPS_SITE_AI_KEY`、`OPS_SITE_SKILLS_URL`、`OPS_ASSISTANT_ROLES`。没有受管 AI 配置时，登录和看板仍可用。通用 MCP 工具列表、调用和参数透传均不提供；日后如需要某个 MCP 能力，单独设计固定业务接口并校验角色和字段。
- 默认事实库 `data/履约单.duckdb`，默认操作库 `data/ops.db`；可在运行环境使用 `OPS_DATA_DIR`、`OPS_FACT_DB`、`OPS_OPS_DB` 覆盖。事实库缺失时停止启动；操作库缺失时才建空库，不创建本地口令账号。升级不搬运、不替换现有库。
- Linux 中枢不启动 Windows 17TRACK 拉取执行器。界面显示不可用，手工提交返回 `503`，不会排队。筛选轨迹分析单次最多处理 10,000 笔；AI 解读只接收去掉样单编号和地点的统计。

## 验收

| 检查 | 预期 |
|---|---|
| 浏览器打开站点 | 证书校验通过，未登录跳 `/login` |
| 中枢授权与回调 | 使用已获授权的测试账户，回到看板，页面 URL 不含授权码 |
| 无效或重放 `state` | 返回 `/login?error=state` |
| 未登录访问 `/api/me` 或轨迹分析 | `401` |
| 旧 MCP 列表和调用 | 路由不存在，已登录与未登录均为 `404`；不发起 MCP 调用 |
| 助手凭据失效 | 向前端返回固定错误描述，不返回凭据或上游认证信息 |
| 业务验证 | 用有权限的测试账号验证看板汇总和必要范围内的查单；检查拉取按钮不可用 |

本地测试使用合成数据、模拟中枢服务和临时证书，不能代替站点上线后的验证。
