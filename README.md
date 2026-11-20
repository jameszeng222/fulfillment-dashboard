# 履约单看板 · 构建说明

## 线上地址

**https://logistics-dashboard.pages.dev**

托管在 Cloudflare Pages，由 `.github/workflows/deploy-pages.yml` 自动发布：
**推一次 `main`，GitHub Actions 自动** `python assemble.py` → 校验产物 → `wrangler pages deploy`，
约 25 秒上线。

需要的两个仓库 Secret（已配置）：`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`。

所以**更新数据只需要**：改完 `dashboard_src/cube.json` 后 `git push`，半分钟内线上就是新的。

> **要看看板，请打开 `履约单看板.html`。**
> `dashboard_src/app.template.html` 是模板，只有占位符没有数据，直接打开会看到一句提示。
> 这是刻意的：模板每次重建都会被覆盖，别收藏它。

`履约单看板.html` 是**单文件、零依赖**的：ECharts 与数据全部内嵌，双击即可打开，
可以直接发微信/邮件。数据更新后按下文重跑两个脚本即可。

## 数据同步（ERP 导出 → 数据库）

从 ERP 下载的 `履约单数据_YYYY_MM_DD_HH_MM.xlsx` 放进 `%USERPROFILE%\Downloads`
或 `Desktop\履约单2026`，然后：

```powershell
python sync.py                # 导入新文件 + 重建看板
python sync.py --dry-run      # 只看会导入哪些
python sync.py --push         # 导入 + 重建 + 自动提交推送（线上自动更新）
```

**已配置 Windows 计划任务「履约单看板同步」，每 30 分钟自动跑一次 `sync.py --push`**，
日志在 `sync.log`。也就是说：**下载文件放进 Downloads，半小时内线上就更新了**，不用管。

去重靠 `sync_ledger` 表按文件内容 sha256 记录，同一个文件改名再放一次也不会重复导入。
合并语义是按「创建日期」区间整体替换，重复导入幂等。

首次上线已用 `python sync.py --seed` 把已有的 29 个文件登记为"库里已有"，不会重跑历史。

## 业务预警（PRD 规则落地）

把《物流轨迹预警监控系统 PRD》的规则算在真实数据上，产出可执行的预警清单。

`powershell
python alerts.py build              # 重算 fct_alert
python alerts.py build --asof 2026-11-16
python alerts.py summary            # 分布统计
python alerts.py list --rule returning --limit 30
python alerts.py export             # 导出 out/履约预警清单.csv
`

**已实现 8 条**（数据支持）：超时未签出、物流未上网、运输超时、物流断更、物流停滞、
海关卡关、派送异常、包裹退运、其他异常。

**未接入 3 条**（缺数据源，不臆造）：履约单报错、商品缺货、拆单异常 —— 需要 ERP 错误码、
库存分配结果和渠道分配结果。

口径要点：

| 项 | 做法 |
|---|---|
| 主预警 | 同一单可命中多条规则，按 PRD 优先级只取一条作主预警，其余为次预警 |
| 运输超时 | 按「仓库×物流商×目的国」历史已签收单的 **P90 尾程天数 + 2 工作日** 判定 |
| 物流未上网 | 只认「有轨迹但停在 InfoReceived/NotFound」；**完全没轨迹的单不报**，那可能是单号没同步进 17track |
| 官方状态 | 预警不改编 17track 主/子状态，只在其上叠加一层业务判断 |
| 时间基准 | 默认取数据最新下单日，可用 --asof 固定以复现 |

## 17track 轨迹（只拉不注册）

单号由 ERP 同步注册到 17track 账号，本脚本只负责**把轨迹和状态拉回来**，
不重复注册。未注册的单号会被平台拒绝（`-18019902`），记录下来跳过。

```powershell
$env:TRACK17_TOKEN = "你的key"        # 只放环境变量
python track17.py plan                 # 看要拉多少、按承运商分布
python track17.py poll --limit 400     # 拉（每批 40 个）
python track17.py poll --days 7        # 只拉最近 7 天创建的单
python track17.py poll --completed --days 7 --limit 40  # 小批回查近期 ERP 已签收单
python track17.py poll --carrier postnet
python track17.py status               # 统计
python track17.py selftest             # 不需要 token，验证解析逻辑
```

**配额**：17track 的配额只用于**注册**，`gettrackinfo` 查询不扣配额，只受 3 req/s 限流。
当前账号剩余配额 100 万+。

**实测的注册覆盖率**（决定能拉回多少）：

| 承运商 | 已注册 | 说明 |
|---|---|---|
| postnet（南非） | **全部** | ERP 同步覆盖，17track 里有真实轨迹 |
| WYT-USPS（美国） | **0** | ERP 没同步这批号 |
| YFH-Logistics | 全部 | |
| DHL | 0 | |

也就是说：**南非线能拉到，美国线拉不到**。要补美国线需要另配注册。

**响应结构**（v2.2）：`milestone[]` 为标准关键节点，`tracking.providers[].events[]` 是承运商逐条回传。

| 字段 | 内容 |
|---|---|
| `track_info.latest_status` | 主状态 / 子状态（9 主 × 30 子） |
| `track_info.latest_event` | 最新事件的时间、描述、地点 |
| `track_info.time_metrics` | `days_of_transit` / `days_after_order` / **`estimated_delivery_date`（AI EDD）** |
| `track_info.milestone[]` | 9 个关键节点：InfoReceived / PickedUp / Departure / Arrival / AvailableForPickup / OutForDelivery / Delivered / Returning / Returned |
| `track_info.tracking.providers[].events[]` | 按承运商返回事件时间、地点与描述。明细写入 `trk_event`，旧履约单可用 `--completed` 回查 |
| `track_info.misc_info.risk_factor` | 风险因子 |

写入的数据表：

| 表 | 内容 |
|---|---|
| `trk_status` | 单号级最新状态、签收时间、运输天数、EDD、风险因子 |
| `trk_milestone` | 9 个关键节点的时间 |
| `trk_event` | 承运商逐条事件，按运单号和查询运输商标识读取 |
| `trk_missing` | 未注册的单号（避免反复查） |

外加视图 `trk_order_link`：把轨迹与履约单左连，算 `承运商尾程天` 和签收对账
（双方已签 / 承运商已签_WMS未签 / WMS已签_承运商未见 / 未注册 / 双方均未签）。

> 注意：该视图是全量左连，未上轨迹的履约单会落在 `WMS已签_承运商未见`，
> 做对账分析时请先按 `轨迹状态 IS NOT NULL` 过滤。

## 目录

```
履约单看板.html                    ← 本地双击用（自动生成，不要手改）
public/index.html                  ← 线上部署入口（自动生成，不要手改）
build_cube.py                      ← 从 DuckDB 聚合成多维立方体
assemble.py                        ← 把立方体 + ECharts 拼进模板
dashboard_src/app.template.html    ← 页面模板（改这里，不是直接看的页面）
dashboard_src/echarts.min.js       ← ECharts 库（已 vendor）
dashboard_src/cube.json            ← 立方体数据（build_cube.py 产出，随代码提交）
reference/平台对照表.xlsx           ← PC 编码 → 平台名称
reference/17track_carriers.json    ← 17track 承运商编码表
DEPLOY.md                          ← 部署方案（上线 / 自动更新 / 17track）
sql/                               ← 数据分层与 17track 数据模型（设计文档性质）
履约单.duckdb                       ← 数据源，509MB，不进 git
```

> 部署相关见 [DEPLOY.md](DEPLOY.md)。**
> `public/index.html` 和 `履约单看板.html` 都是 `assemble.py` 生成的，不要直接改。

## 重建

```powershell
python build_cube.py     # 从库重新聚合，会打印口径校验值
python assemble.py       # 组装单文件
```

`build_cube.py` 结束时会打印四个核心口径，应当与看板 KPI 一致：

| 指标 | 期望值（2026-01-01 ~ 11-12） |
|---|---|
| 履约单数 | 1,325,618 |
| 总件数 | 2,095,757 |
| 仓内处理时长 | 24.08 h |
| 物流运输天数 | 3.58 d |
| 签收覆盖率 | 93.1% |

数字对不上说明数据变了或口径被改，先查这两处。

## 平台名称

`reference/平台对照表.xlsx` 定义 `平台code → 平台名称`，看板里显示的是名称而不是 PC 编码。
对照表里若有编码缺失，`build_cube.py` 会在 stderr 打警告并回退显示原编码——
新增平台时记得补这张表，否则新平台会以 `PCxx` 形式出现。

当前库里的 18 个平台全部命中（对照表共 25 条，PC4/PC6/PC14/PC22/PC23/PC25/PC26 暂无数据）。

## 筛选维度

| 维度 | 多选 | 说明 |
|---|---|---|
| 时间 | 单选 | 月份芯片，另有「全部」 |
| 仓库 | ✓ | 13 个 |
| 平台 | ✓ | 18 个，显示平台名称 |
| 后台物流商 | ✓ | 19 个，系统里的签约承运商 |
| 国家 | ✓ | 109 个，下拉内可搜索 |

「后台物流商」和「C 端显示物流服务商」是两回事，不要混用：
前者是与我方签约的总包（如 `WYT-USPS`），后者才是客户看到的末端派送商
（GOFO / SpeedX / USPS / OnTrac …）。一个后台承运商底下可以挂十几个末端。

## 筛选是怎么实现的

数据不是按行存的，而是预聚合成 4 个立方体，全部存在**可加和的整数**上
（单量、件数、各种求和与计数）。平均值（仓内/物流/签收率）一律在浏览器里
用「和 ÷ 数」现算 —— 这样任意筛选组合叠加都不会有精度损失。

前 5 个索引固定是筛选维度 `mi wi pi ci ni`（月/仓/平台/后台物流商/国家），后面接度量：

| 立方体 | 布局 | 行数 | 支撑的图 |
|---|---|---|---|
| `day` | 日×仓×平台×物流商×国家 + 7 度量 | 17,428 | KPI、趋势、仓库/平台/物流商/国家 排行 |
| `ccar` | 月×仓×平台×物流商×国家×C端 + 3 度量 | 3,659 | C 端显示物流服务商 |
| `status` | …×出库单状态 + 单量 | 4,457 | 状态分布 |
| `aging` | …×仓内时效档 + 单量 | 6,759 | 仓内时效分布 |

**为什么不再加筛选维度**：每多一个维度，立方体按 3~10 倍膨胀。现在 cube.json 约 0.8MB，
产物 1.9MB；再加一个维度会到十几 MB，浏览器打开会明显卡。

**样本量保护**：有效样本不足 30 单时不给均值，显示 `—`。例如「C 端承运商=未填」有 2.9 万单，
但只有 1 张已签收，算出来的「2.1 天」纯属噪声。

## 口径定义

| 指标 | 算法 |
|---|---|
| 单量 | `count(DISTINCT 履约单号)`，多 SKU 一单只计 1 |
| 件数 | `sum(数量)` |
| 仓内 | 单级 `签出时间 − 创建时间`，只对 >0 取均值 |
| 物流 | 单级 `签收时间 − 签出时间`，只对 >0 取均值 |
| 签收率 | 有签收时间的单 ÷ 总单 |

**平均计费重已移除**：该字段 49.6%（65.8 万单）未填写，均值只代表填了的那一半，
分布图的第一个柱子也永远是「未填」657k，没有决策价值。要恢复的话在
`build_cube.py` 里把 `w`/`wv`/`wb` 与 `wS`/`wC` 度量加回去，再在模板里复原卡片即可。

C 端承运商名称做了归一：`FedEx/Fedex/fedex`、`FedEx SmartPost/Fedex SmartPost`、
`Off-Line logistics/Offline Logistics`、`UPS Mail lnnovations`（原拼写错误）、
`amazon/Amazon Logistics` 都已合并。

## 筛选状态进 URL

筛选结果会写到地址栏，例如：

```
履约单看板.html?m=10&wh=0&pf=0&cn=3&cr=2
```

把带参数的链接发给同事，对方打开就是同一个视图。

## 改样式/加图

只改 `dashboard_src/app.template.html`，然后跑 `python assemble.py`。
不要直接改 `履约单看板.html`，下次重建会被覆盖。

`assemble.py` 会检查两个占位符各出现且仅出现一次——提示文案里如果写了占位符字面量，
数据会被注入多次，文件直接翻倍，脚本会拦住并报错。

## 履约监控系统（服务版）

上面那个单文件看板是**只读快照**。要做"谁在处理哪张单"这类事，用系统版：

### 本机预览

```powershell
python preview.py --data-dir "D:\履约数据"
```

打开 `http://127.0.0.1:8766/preview`。数据目录应包含 `履约单.duckdb` 和 `ops.db`。
预览读取源履约数据，首次启动时将操作库复制到当前项目。认领、归档、备注等操作写入预览操作库，重启后仍可查询。
预览入口仅在上述本机启动模式提供。常规服务使用下方登录流程。
可用 `--facts-file` 读取一份包含轨迹回查结果的独立履约数据库，同时继续使用当前项目的预览操作库。

### 本机数据库与更新

以下命令在履约监控系统的项目目录（含 `preview.py`）运行：

| 文件 | 用途 |
|---|---|
| 桌面 `履约单2026` | 新 ERP Excel 导出的投放文件夹 |
| 当前项目的 `履约单.duckdb` | 履约明细、汇总、滚动查询的 17TRACK 事件（`trk_event`）、历史逐条轨迹（`trk_archive_event`）及索引 |
| 当前项目的 `ops.db` | 本地 SLA、预警规则、认领、归档、备注及任务日志 |
| 当前项目的 `17TRACK历史轨迹.sqlite3` | 历史回补程序的断点进度与导入源。成功导入后，网页从 DuckDB 读取轨迹 |
| 桌面 `履约单和轨迹分析2026\履约单.duckdb` | 原静态看板的独立数据文件 |

原看板的 Windows 任务每 30 分钟扫描「下载」和桌面的「履约单2026」文件夹，按文件指纹导入新 ERP 导出。任务调用 `sync.py --push`，有新数据时会重建并发布原静态看板。

本机履约监控系统由 `sync_control_tower.ps1` 更新，运行日志在项目目录的 `rolling-sync.log`。Windows 计划任务每 5 分钟扫描桌面「履约单2026」中的 `履约单数据_*.xlsx` 等支持的 Excel 格式。按文件内容去重，按履约单号、SKU、批次合并。同一明细的导出时间可从文件名识别时保留时间较新的版本。重建统计和预警后切换新数据，上一个数据库保存在 `履约单.previous.duckdb`。正在打开的网页刷新后可查看结果，页面右上角显示建单截止日期和最近导入时间，「数据与规则」保留导入记录。

```powershell
.\sync_control_tower.ps1                 # 立即检查 ERP 导出
.\sync_control_tower.ps1 -TrackLimit 4000 # 立即检查近期轨迹
.\sync_control_tower.ps1 -ImportArchive  # 将历史回补文件的逐条轨迹和索引导入 DuckDB
```

17TRACK 计划任务每 2 小时分批检查近期运单，每次最多 4,000 个，单号至少间隔 6 小时再查。使用已登记的运单号和实际承运商代码读取逐条事件，跳过 Postnet。任务凭据存放在当前 Windows 用户的加密本地文件中。两个任务均在该 Windows 用户登录期间运行。准备数据期间保持页面可用，替换数据库时会短暂重启正在运行的预览。

「数据与规则 → 17TRACK 同步」显示定时拉取的最近成功时间、下次计划、最近批次结果，以及 Webhook 的收件和入库时间。打开该页时每 30 秒读取状态。拉取、收件、入库由本机程序负责，浏览器关闭时也可运行。

Webhook 接收路径是 `POST /api/17track/webhook`。部署时只将该路径通过可信反向代理发布为公网 HTTPS 地址，把完整地址设置为服务进程的 `TRACK17_WEBHOOK_URL`，再到 [17TRACK 后台设置](https://api.17track.net/admin/settings) 保存并测试。当前 Windows 用户的本地加密凭据可直接验签，其他部署可在服务端用 `TRACK17_WEBHOOK_KEY` 或 `TRACK17_TOKEN` 提供对应的 API 密钥。

推送验签后进入 `ops.db` 的持久队列。现有的五分钟任务在检查履约明细时同时处理推送。按运单号匹配，已确认承运商代码的单继续使用该代码，避免旧推送覆盖新状态。Webhook 到得比 ERP 导出早时会暂存，最长等待 24 小时。轨迹发布成功后，收件记录保留用于查验，收件内容从队列中清除。

支付时间（客户下单）、履约单创建时间、签出时间（出库）均为北京时间。妥投时效、P90 和 SLA 优先使用 17TRACK 确认的妥投时间。缺少该时间时，按仓库的 WMS 导出时间口径补算。JY01、XC01 等北京时间仓直接使用原始时间，海外 WINIT 按收货地时区及当地夏令时换算。下单和出库使用 ERP 起点，上网使用首条有效揽收或运输扫描。缺少可信妥投时间或有效起点的履约单不进入相应口径的时效样本。详情保留 WMS 原始时间并显示妥投统计来源。17TRACK 轨迹和妥投在网页上用承运商当地时间显示，缺当地时间时标注 UTC。

`reference/us-postal-timezones.csv` 提取了 [Ready APIs curated-us-zips](https://github.com/ReadyAPIs-com/curated-us-zips) 数据的邮编、州和 IANA 时区三列并按邮编排序，按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 使用。加拿大部分地区采用 [Canada Post 邮编区域](https://www.canadapost-postescanada.ca/cpc/en/support/articles/addressing-guidelines/postal-codes.page) 与 [加拿大国家研究委员会时区资料](https://nrc.canada.ca/en/certifications-evaluations-standards/canadas-official-time/time-zones-daylight-saving-time)。

查询新的 17TRACK 运单可在预览退出期间，从当前项目执行 `python track17.py poll --days 7 --carrier WYT-USPS`。密钥从 `TRACK17_TOKEN` 读取。该命令按所选渠道查询已有登记，并将结果写入当前项目的 DuckDB。

页面布局在 `templates/app.html`，样式和交互在 `static/app.css`、`static/app.js`。这些文件修改后刷新浏览器。修改 `app/` 中的 Python 服务端代码后，重启本地预览服务以加载改动。

### 历史轨迹离线回补

```powershell
python backfill_tracking.py --facts "D:\履约数据\履约单.duckdb" --output "17TRACK历史轨迹.sqlite3" --since 2026-07-21 --through 2026-11-18
```

使用 `TRACK17_TOKEN` 环境变量。程序按 17TRACK 登记列表取得运输商代码，再查询已有运单的逐条事件。
SQLite 中的 `packages` 保存每单查询进度，`tracking_details` 保存每个运单号与运输商代码的查询结果，
`tracking_events` 保存逐条事件。中断后以相同数据源、日期和输出文件启动即可继续。

回补后，从当前项目目录运行 `.\sync_control_tower.ps1 -ImportArchive`。程序将一致的 SQLite 快照分批导入 DuckDB，核对逐条事件行数，并按履约单和运单号重建索引。发布快照时会短暂重启正在运行的预览。详情使用 DuckDB 中的近期和历史事件合并显示。备份业务数据可保存 `履约单.duckdb` 与 `ops.db`，要接续批量回补时也保留历史 SQLite 文件。

加上 `--all-waybills` 可按运单号补查缺少逐条事件的履约单，范围日期优先取签收日，其次取签出日或履约单创建日，不按 ERP 状态筛选。已有结果沿用同一个 SQLite 文件。ERP 运单号末尾的 `+数字` 只用于保留原始显示，查询使用主运单号。回补通过 17TRACK 登记列表取得实际运输商代码，只读取当前账号里的记录。

```powershell
python init_ops.py          # 建操作库 ops.db（首次）
python build_orders.py      # 物化订单级事实表 fct_order
python alerts.py build      # 重算预警并镜像到 ops.db
python -m uvicorn app.main:app --port 8000
```

打开 http://127.0.0.1:8000 ，默认账号：

| 账号 | 密码 | 角色 |
|---|---|---|
| admin | admin123 | 系统管理员 |
| wuliu | wuliu123 | 物流专员 |
| kefu | kefu123 | 客服专员 |

> 上线前务必改掉这三个密码。

### 架构

| 层 | 实现 | 说明 |
|---|---|---|
| 事实层 | DuckDB `履约单.duckdb` | 只读。订单、轨迹、预警计算结果，由管道重建 |
| 操作层 | SQLite `ops.db` | 可写。用户、会话、预警处理状态、备注、审计日志 |
| 服务层 | FastAPI | 服务端分页筛选，前端不再下载全量数据 |
| 界面层 | 服务端渲染 + 原生 JS | 四个工作区：总览 / 履约单 / 预警处理 / 轨迹追踪 |

**关键设计**：预警明细（`alerts` 表）由管道从 DuckDB 镜像到 SQLite，
处理状态（`alert_state`）以 `(履约单号, 规则)` 为键独立保存。
所以**重算预警不会冲掉人已经处理过的记录**——这是系统和看板的本质区别。

### API

| 接口 | 用途 |
|---|---|
| `POST /api/login` `POST /api/logout` `GET /api/me` | 会话 |
| `GET /api/meta` | 筛选项（仓库/平台/物流商/国家/月份/规则） |
| `GET /api/orders` | 订单分页查询，支持 q/wh/pf/cr/cn/status/month/signed/tracked |
| `GET /api/orders/{no}` | 详情：SKU、轨迹节点、预警、备注 |
| `POST /api/orders/{no}/notes` | 写备注 |
| `GET /api/alerts` | 预警分页查询，支持 rule/level/state/assignee/wh/cr/cn |
| `POST /api/alerts/action` | 认领 / 处理中 / 已解决 / 归档 / 重开 |
| `GET /api/stats` `GET /api/health` | 统计与健康 |

交互式文档在 `/api/docs`。

### 还没做的

- 定时任务（ERP 同步、17track 拉取、预警重算）——目前需手动跑
- 角色数据范围（物流部只看自己团队）
- 部署方案（Docker / 服务器）

### 四个业务页面

| 页面 | 职责 | 关键能力 |
|---|---|---|
| **数据总览** | 看全局 | 6 个 KPI（单量/件数/签收率/仓内/运输/6天达成率）、月度趋势、物流渠道占比、发货仓分布、目的国家分布、出库单状态；筛选支持日期区间 + 月份 + 仓库 + 平台 + 物流商 |
| **轨迹监控** | 看每一单的轨迹 | 履约单/运单号搜索；按月份、仓库、物流商、17track 官方状态、自定义物流异常筛选；列表同屏展示官方状态（主+子）与业务异常；点开看轨迹时间轴与处理动作 |
| **渠道时效** | 比渠道 | 各渠道平均妥投、P90、3/5/6 天达成率、SLA 目标、在途超时数、达标结论；渠道 × 国家明细 |
| **数据与规则** | 查口径 | 9 条预警规则的触发条件/判定依据/排除条件/恢复方式（含命中量）；渠道 SLA 表（承诺时效 + 各档目标 + 实际达成）；未接入规则明确列出 |

规则与 SLA 定义在 `app/rules.py`，**页面展示与引擎判定共用同一份定义**，不会出现"文档写一套、代码跑另一套"。
