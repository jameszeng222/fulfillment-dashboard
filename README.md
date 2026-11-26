# 履约监控系统 · 智能中枢版

中枢站点：[logistics-dashboard](https://biz.htwig.com:18700/) · [发布与验收](deploy/README.md) · [运维交接](交接文档.md)

| 页面 | 用途 |
|---|---|
| 数据总览 | 查看日单量、月度时效、妥投覆盖和渠道单量。 |
| 轨迹监控 | 按履约单号、运单号、订单号或平台订单号查单；选定日期或单号后可分析聚集异常。 |
| 渠道时效 | 对比支付、出库、上网到妥投的时间及 SLA 达成情况。 |
| 数据与规则 | 查看 ERP 和 17TRACK 的数据状态，管理业务预警与各渠道 SLA。 |

中枢站点使用智能中枢登录。`data/履约单.duckdb` 是只读事实库，`data/ops.db` 是该站点的操作库，保存会话、规则和处理记录。不会因同步代码而同步两站的数据库、账号或配置。Windows 批量拉取工具留在数据源侧；中枢页面不提交此类任务。

## 两条发布线

- **`main` → Cloudflare**：共享业务功能先在 `main` 开发，Cloudflare 仅按其自己的流程发布。
- **`ops/logistics-dashboard-hub` → 智能中枢**：通过 PR 单向合入 `main`，保留中枢登录、直接 HTTPS 和站点数据目录。不把中枢适配反向合入 `main`，不携带 Cloudflare 的发布、转发及隧道配置。两站分别验收和发布。

## 开发验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s test -p 'test_*.py' -v
python -m unittest discover -s test -p 'ops_fastapi.py' -v
node --check static/app.js
```

`deploy/run_site.py` 是中枢发布入口。使用平台受管运行环境启动；`--check` 仅诊断配置是否存在，不代替 HTTPS 或登录验收。
