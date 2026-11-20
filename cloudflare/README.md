# 履约监控系统在 Cloudflare Pages 上的入口

GitHub Actions 发布独立的 `fulfillment-monitor` Pages 项目，并将当前网页和脚本一起发布至边缘。
访客页面、总览与标准时效统计、完整单号和轨迹时间线可从云端只读快照读取。原静态站点已下线，旧地址返回 410。

## 访客快照

发布流程为 Pages 绑定私有 Workers KV 命名空间 `fulfillment-monitor-preview`。首次发布需要部署凭据有 Workers KV Storage 写入权限。也可以预先创建命名空间，将其 ID 写入 GitHub 变量 `FULFILLMENT_SNAPSHOT_KV_ID`。

Windows 任务 `履约监控-Cloudflare-快照同步` 登录时检查快照，每六小时检查一次本机履约数据和轨迹。手动同步运行 `.\cloudflare\run_local.ps1 -Component Snapshot`，日志在 `%LOCALAPPDATA%\FulfillmentMonitorCloud\snapshot-sync.log`。查询云端发布状态可访问 `/__edge/health`，其中 `snapshotAt` 是当前快照时间。

发布脚本生成分片的单号索引、脱敏履约记录和物流事件，连同总览及三种时效口径的全量统计存入 KV。变化分片上传完成后才切换版本，保留前一份供回退。按计划运行时每次最多上传 193 个数据块和一份清单。可用 `python -m cloudflare.publish_snapshot --check` 在本机核对大小。

开放测试时，线上请求优先使用本机数据。本机离线后，线上入口显示最后发布的只读快照与同步时间，访客可看全量统计并按完整单号查轨迹。组合筛选、完整订单详情和修改需要本机在线。

## Windows 私有连接

`fulfillment-monitor-origin` Tunnel 只向 Workers VPC 发布 `127.0.0.1:8770`。
Actions 首次部署无公网路由的源站 Worker。Cloudflare 管理界面上的 VPC 绑定单独保留，
GitHub 变量 `FULFILLMENT_PRIVATE_WORKER_BOUND` 设为 `true` 后 Actions 会为 Pages 绑定这个 Worker。
来源密钥从 GitHub Secret 读取。本机使用当前 Windows 用户的
加密凭据，文件保存在 `%LOCALAPPDATA%\FulfillmentMonitorCloud`。

开发期间直接打开 [线上入口](https://fulfillment-monitor.pages.dev/) 或 [本机入口](http://127.0.0.1:8766/)。源站在 `cloudflare/run_local.ps1` 中设置 `FULFILLMENT_OPEN_TEST`，Pages 在部署工作流中设置 `FULFILLMENT_EDGE_OPEN_TEST`，本机 `preview.py` 默认启用开放测试。

本机分别运行 `.\cloudflare\run_local.ps1 -Component Tunnel` 和
`.\cloudflare\run_local.ps1 -Component Origin`。可由 Windows 登录任务启动两项服务。

## 使用已配置的 HTTPS 源站

1. 在运行履约数据库的机器上，把服务端环境变量 `OPS_SITE_ORIGIN` 设为 Pages 的实际 HTTPS 站点地址。
   设置随机的 `FULFILLMENT_EDGE_KEY`，它应由至少 32 个随机字节进行 URL 安全 Base64 编码。
   开发期间可设置 `FULFILLMENT_OPEN_TEST=true`。启用正式登录时，同步关闭边缘与源站的开放测试设置，并配置正式站内账号或 OAuth 身份映射。
2. 在拥有数据库文件的项目目录启动真实入口。
   `python -m uvicorn app.cloudflare_origin:application --host 127.0.0.1 --port 8770 --lifespan on --no-access-log`。
   让受管 Cloudflare Tunnel 的独立 HTTPS 主机名转发到 `http://127.0.0.1:8770`。
3. 在 Pages 的生产环境配置 `FULFILLMENT_ORIGIN_URL` 为 Tunnel 的 HTTPS 主机名，
   并用加密变量写入同一个 `FULFILLMENT_EDGE_KEY`。如果使用智能中枢登录，再配置
   `OPS_AUTHORIZE_URL`，在中枢登记实际线上地址及 `/ops/auth/callback`。
4. 执行 `deploy-fulfillment-monitor.yml`。确认 `/__edge/health` 返回
   `originConfigured: true`，随后按当前访问模式验证首页、订单详情、预警和规则。健康页只显示边缘配置，
   浏览器联调才确认数据服务可用。

本地 Windows 更新任务仍负责导入履约单和接收轨迹。主库、管理操作库、原始轨迹描述和连接密钥由本机保管。
