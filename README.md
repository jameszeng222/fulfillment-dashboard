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

## 17track 轨迹接入

```powershell
$env:TRACK17_TOKEN = "你的token"     # 只放环境变量，别写进文件
python track17.py plan               # 看要注册多少、按承运商分布
python track17.py register --limit 500   # 先小批量试
python track17.py poll --limit 500
python track17.py status
python track17.py selftest           # 不需要 token，验证解析逻辑
```

**计费模型**：按注册单号数计费，注册成功后自动跟踪不再扣费，
所以脚本只对「未签收、未取消、有单号、未注册过」的单号发起注册，一个单号只注册一次。

当前待注册 **40,919** 个，其中 `postnet`（南非）占 32,825——正是 WMS 签收率 0% 的那条线。

落两张表：`trk_subscription`（注册台账 + 最新状态）、`trk_event`（轨迹事件），
外加视图 `trk_order_link` 做 WMS 签收与承运商签收的对账。

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
