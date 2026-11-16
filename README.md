# ETF 资金流向 → 飞书每日推送

<p>
  <img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Wind866/etf-feishu-push/ci.yml?label=CI">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11-3776AB">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-3DA639">
  <img alt="Push" src="https://img.shields.io/badge/push-Feishu-00B0F0">
</p>

> 每交易日 20:03（北京时间）汇总全市场 ETF 主力资金净流入，按 8 个宽基指数出图并推送飞书。无服务器、无数据库，历史数据存于仓库 `data/history.json`。

![推送效果](docs/preview.png)

## 它做什么

- **全市场 ETF 扫描**：抓当日所有 ETF 的主力净流入/流出，按跟踪指数归并成 8 个宽基维度。
- **近 10 日对照**：把当天结果写回 `data/history.json`，图上直接给出近 10 日趋势，单日波动不再误导判断。
- **三级降级推送**：优先图片消息（需自建应用），失败降级卡片，再失败降级文本卡片——任何一级都不至于让群里收不到消息。
- **历史可回补**：`backfill.py` 可按区间补数，断档不用手工拼。

## 快速开始

### 1. 拿到代码

```bash
git clone https://github.com/Wind866/etf-feishu-push.git
cd etf-feishu-push
```

### 2. 配置 Secrets

仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 必填 | 用途 |
| --- | --- | --- |
| `FEISHU_WEBHOOK` | 是 | 群机器人 Webhook |
| `FEISHU_SECRET` | 否 | 加签密钥，开启加签时必填 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 否 | 自建应用凭证；发**图片**消息必需（需开通 `im:resource`） |
| `ALERT_WEBHOOK` | 建议 | 独立告警机器人 |
| `ALERT_SECRET` | 否 | 告警机器人加签密钥 |
| `EM_PROXY_URL` / `EM_PROXY_TOKEN` | 否 | 东财转发代理及其共享密钥，用于绕开限流 |

### 3. 手动验证一次

**Actions → ETF 资金流向 · 每交易日 20:03 推送飞书 → Run workflow**，可选 `mode` / `days`（`days` 为自由文本，工作流内部会做校验，不接受任意值）。

## 运行节奏

| 北京时间 | 任务 | 说明 |
| --- | --- | --- |
| 20:03（交易日） | 主档：抓取 + 出图 + 推送 | `daily-push.yml` |
| 21:03 / 22:03 | cron 兜底 | 按数据日期判重，不重复推送 |
| 断档时 | 自动派发 `backfill` 作业 | `push.py` 留标记，主流程据此触发独立回补 |

- 推送成功后把 `data/history.json` 提交回仓库，多日对照才成立。
- 图片消息需要飞书自建应用开通 `im:resource`；未配置时自动降级为卡片消息。

## 目录结构

```text
push.py                 主流程：抓数 → 聚合 → 出图 → 推送
backfill.py             历史补数
etfpush/
  config.py             环境变量与常量
  sources.py            东财数据源（含代理与重试）
  aggregate.py          按指数归并、口径计算
  render.py             近 10 日对照图（Pillow）
  feishu.py             签名、上传图片、三级降级推送
  history.py            历史读写与 90 日保留
data/
  etf_codes.txt         ETF 代码表
  history.json          历史记录（保留 90 个交易日）
  demo_history.json     演示数据
worker/                 可选：东财转发代理（Cloudflare Worker）
tests/                  CI 门禁：语法 / 单元 / 渲染 / 配置
```

## 本地开发

```bash
pip install -r requirements.txt
pip install -r requirements-optional.txt   # 可选增强

python push.py --demo        # 用演示数据看样式，不抓数、不推送
python push.py --dry-run     # 真实抓数，不推送
python -m unittest discover -s tests
```

## 数据与配置口径

| 项 | 说明 |
| --- | --- |
| 统计口径 | 东财「主力净流入」，按 ETF 跟踪指数归并；同指数多只 ETF 相加 |
| 历史保留 | `data/history.json` 保留最近 90 个交易日，超出自动裁剪 |
| 依赖 | `requests` / `Pillow`，无 pandas、无数据库 |
| 代理 | `EM_PROXY_URL` 与 Worker 的 `PROXY_TOKEN` 必须一致，不一致会一直走直连 |

## 安全说明

- Webhook、加签密钥、App Secret、代理令牌**只**通过 Secrets 提供，仓库内不落地任何凭据。
- 加签要么两个机器人都开，要么都不开；`19021` 表示加签校验失败。
- 代理 Worker 的 `PROXY_TOKEN` 一旦泄露需立即轮换：它等同于免限流的东财入口。
- 所有 Action 固定到 commit SHA，并由 dependabot 每周提升级 PR。

## 排错

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 只收到卡片、没有图片 | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 未配，或应用缺 `im:resource` | 补配并授权，否则长期走卡片降级 |
| 飞书返回 19021 | 加签密钥未配或配错 | 补配 `FEISHU_SECRET` / `ALERT_SECRET` |
| 接口被限流 | 直连东财触发频控 | 配 `EM_PROXY_URL` / `EM_PROXY_TOKEN` 走代理，失败会自动回退直连 |
| 图上没有近 10 日对比 | `data/history.json` 未提交或刚重置 | 跑一次 `backfill.py` 补齐 |

## 相关文档

- [统计口径、数据源与完整排错](docs/README-full.md)
- [东财转发代理](worker/wrangler.toml)

## 免责声明

本工具仅用于信息聚合与研究复盘，不构成投资建议。资金流数据来自第三方接口，可能存在延迟或口径差异。市场有风险，投资需谨慎。

## License

MIT
