# 履约监控系统

[打开线上履约监控系统](https://fulfillment-monitor.pages.dev/) · [部署记录](https://github.com/jameszeng222/fulfillment-dashboard/actions) · [运维交接文档](交接文档.md)

| 页面 | 用途 |
|---|---|
| 数据总览 | 查看日单量、月度时效、妥投覆盖和渠道单量。取消单仅计入出库单状态统计。 |
| 轨迹监控 | 按履约单号、运单号、订单号或平台订单号查单，查看履约节点、逐条物流轨迹及预警。 |
| 渠道时效 | 按 C 端显示物流渠道比较下单、出库、上网到妥投的时效及 SLA 达成。 |
| 数据与规则 | 查看 ERP 导入和 17TRACK 同步状态，管理业务预警与各当前渠道的 SLA。 |

## 日常使用

- 开发期间启用开放测试。直接打开线上链接或 [本机入口](http://127.0.0.1:8766/) 即可浏览履约单、看完整详情、导出、处理预警和配置规则。无需账号、邀请或桌面登录。
- 开放测试中的编辑保存到实际操作库，操作日志使用 `open-test` 记录。线上与本机使用同一份主库与操作库。
- 本机离线期间，线上入口提供 Cloudflare 里的只读快照。页面显示快照时间，支持看全量统计和按完整单号查已入库轨迹。

## 数据更新和备份

| 内容 | 怎么更新 |
|---|---|
| ERP 履约单 | 将 ERP 导出的 `履约单数据_*.xlsx` 放进桌面的 **履约单2026** 文件夹。Windows 每 5 分钟检查，按文件内容去重并合并明细。 |
| 近期 17TRACK 轨迹 | Windows 每 2 小时查询一批已登记运单，每批最多 4,000 个，按运单号和承运商代码匹配，跳过 Postnet。 |
| 历史轨迹 | 历史回补写入本机断点库。回补后执行 `.\sync_control_tower.ps1 -ImportArchive`，将逐条轨迹导入主库。 |
| 同步状态 | 在 **数据与规则 → 数据健康 / 17TRACK 同步** 查看导入、拉取和 Webhook 收件与入库。 |

业务备份保留项目目录内的 **`履约单.duckdb`** 和 **`ops.db`**。前者保存履约单及近期和历史逐条轨迹。后者保存 SLA、预警、操作记录和同步队列。如需继续历史回补，再保留 **`17TRACK历史轨迹.sqlite3`**。数据和密钥由运行环境保管。

## 时效口径

支付时间是客户下单时间，创建时间是履约单创建时间，签出时间是出库时间，签收时间是物流妥投时间。时效优先使用 17TRACK 妥投，缺失时按仓库口径用 WMS 补算。国内仓的 WMS 原值按北京时间，海外 WINIT 按收货地当地时区及夏令时换算。页面保留承运商当地时间，计算时统一时区。

## 开发和发布

使用 Python 3.12 和 Node.js 24，在项目目录运行：

~~~powershell
python -m pip install -r requirements.txt
python -m unittest discover -s test -p ops_fastapi.py -v
node --test test/cloudflare-edge.mjs
~~~

- 界面在 `templates/` 和 `static/`，应用接口在 `app/`，滚动更新入口是 `sync_control_tower.ps1`。
- **`main` 是正式发布分支**。发布相关文件更新后，由 [GitHub Actions](https://github.com/jameszeng222/fulfillment-dashboard/actions/workflows/deploy-fulfillment-monitor.yml) 部署 Cloudflare Pages。
- 修改本机 Python API 后，同时更新生产电脑上的工作区并重启 Origin 任务。边缘发布与本机数据服务分别验收。
- 原静态看板已退役。详细的任务配置、备份和故障排查见[交接文档](交接文档.md)。
