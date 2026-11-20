# 履约监控系统在 Cloudflare Pages 上的入口

GitHub Actions 发布独立的 `fulfillment-monitor` Pages 项目，只上传 `cloudflare/pages`。
此入口转发完整监控系统的页面和接口。原静态站点已下线，旧地址返回 410。

## Windows 私有连接

`fulfillment-monitor-origin` Tunnel 只向 Workers VPC 发布 `127.0.0.1:8770`。
Actions 首次部署无公网路由的源站 Worker。Cloudflare 管理界面上的 VPC 绑定单独保留，
GitHub 变量 `FULFILLMENT_PRIVATE_WORKER_BOUND` 设为 `true` 后 Actions 会为 Pages 绑定这个 Worker。
来源密钥从 GitHub Secret 读取。本机使用当前 Windows 用户的
加密凭据，文件保存在 `%LOCALAPPDATA%\FulfillmentMonitorCloud`。

从本机桌面打开「履约监控 - 免密码登录」，或从当前项目目录运行
`.\cloudflare\run_local.ps1 -Component Login`。该命令为专用账号 `fulfillment-online`
生成一次性登录链接，有效期 90 秒。线上实例不启用密码登录，原本地账号照常使用。

本机分别运行 `.\cloudflare\run_local.ps1 -Component Tunnel` 和
`.\cloudflare\run_local.ps1 -Component Origin`。可由 Windows 登录任务启动两项服务。

## 使用已配置的 HTTPS 源站

1. 在运行履约数据库的机器上，把服务端环境变量 `OPS_SITE_ORIGIN` 设为 Pages 的实际 HTTPS 站点地址。
   设置随机的 `FULFILLMENT_EDGE_KEY`，它应由至少 32 个随机字节进行 URL 安全 Base64 编码。
   配置正式站内账号，或为此线上地址配置 OAuth 及身份映射。默认示例密码会导致线上服务拒绝启动。
2. 在拥有数据库文件的项目目录启动真实入口。
   `python -m uvicorn app.cloudflare_origin:application --host 127.0.0.1 --port 8770 --lifespan on --no-access-log`。
   让受管 Cloudflare Tunnel 的独立 HTTPS 主机名转发到 `http://127.0.0.1:8770`。
3. 在 Pages 的生产环境配置 `FULFILLMENT_ORIGIN_URL` 为 Tunnel 的 HTTPS 主机名，
   并用加密变量写入同一个 `FULFILLMENT_EDGE_KEY`。如果使用智能中枢登录，再配置
   `OPS_AUTHORIZE_URL`，在中枢登记实际线上地址及 `/ops/auth/callback`。
4. 执行 `deploy-fulfillment-monitor.yml`。确认 `/__edge/health` 返回
   `originConfigured: true`，随后用正式账号验证首页、订单详情、预警和退出。健康页只显示边缘配置，
   浏览器联调才确认数据服务可用。

本地 Windows 更新任务可继续运行。完整网页的可用性取决于本地数据服务和 Tunnel。
数据库、用户操作库、轨迹明细和连接密钥均由各自运行环境保管。
