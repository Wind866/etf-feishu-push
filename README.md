# ETF 资金流向 → 飞书每日推送

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

每个 **A 股交易日 20:00（北京时间）** 自动统计全市场 ETF 的**主力资金净流入**，按 8 个宽基指数汇总成一张表，以**图片消息**推到飞书群。

> 用 GitHub Actions 跑，不需要服务器、不需要数据库，历史数据直接存仓库里的 `data/history.json`。

![预览](docs/preview.png)

---

## 它是怎么工作的

```
每交易日 20:00 (UTC 12:00)
        │
        ├─ 交易日判定   取上证指数最新行情时间戳，节假日/周末自动跳过
        ├─ 抓取数据     天天基金代码表 → 东财 ulist 批量取 f62（主力净流入）
        ├─ 归类汇总     8 个宽基指数 × 近 10 个交易日，元 → 亿元
        ├─ 写入历史     data/history.json（Actions 自动 commit 回仓库）
        └─ 出图并推送   PIL 画表 → 飞书图片消息（无应用凭证时降级为卡片）
```

## 快速开始（3 步）

### 1. Fork 本仓库

### 2. 配置飞书群机器人

飞书群 → 右上角 `···` → **设置** → **群机器人** → **添加机器人** → **自定义机器人**：

- 复制 **Webhook 地址**（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`）
- 建议勾选**签名校验**并复制密钥（更安全，防止 webhook 被滥用）

### 3. 填 Secrets

仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

| Name | 必需 | 说明 |
| --- | --- | --- |
| `FEISHU_WEBHOOK` | ✅ | 群机器人 Webhook 地址 |
| `FEISHU_SECRET` | 可选 | 机器人签名密钥（开了签名校验就必填） |
| `FEISHU_APP_ID` | 可选 | 飞书自建应用 App ID，**发图片消息必需** |
| `FEISHU_APP_SECRET` | 可选 | 飞书自建应用 App Secret |

配好之后进 **Actions** → `ETF 资金流向 · 每交易日 20:00 推送飞书` → **Run workflow** 手动跑一次验证。

### 关于图片消息（可选，但强烈建议）

群机器人的 webhook **不能直接上传图片**，必须由飞书自建应用上传拿到 `image_key` 再由机器人发出。所以想要参考图那种图片效果，需要：

1. [飞书开放平台](https://open.feishu.cn/app) → 创建**企业自建应用**
2. 开通权限 `im:resource`（上传图片）
3. 把 `App ID` / `App Secret` 填进 Secrets

> 不配也能用：脚本会自动降级为**卡片消息**（飞书卡片 2.0 原生表格，红绿着色，手机上同样可读）。卡片失败还会再降级成逐行文本卡片。

---

## 统计口径

| 项目 | 说明 |
| --- | --- |
| 指标 | 东财「主力净流入-净额」，逐只 ETF 求和后换算成**亿元** |
| 颜色 | **正 = 净流入（红）/ 负 = 净流出（绿）**，符合 A 股习惯 |
| `所有ETF` | 全市场 ETF 合计，**剔除货币/现金管理类**（其资金流来自申赎套利，混进来会出现几十亿的假流入） |
| 宽基归列 | 只统计**纯被动跟踪**该指数的 ETF |

### 归列规则（为什么不用简单关键词）

名字里带「包含」就归类的做法会大规模误判，实测踩过的坑：

| 误判 | 后果 |
| --- | --- |
| `创业板50ETF`、`创业板新能源ETF`、`创业板人工智能ETF` | 全被算进「创业板指」 |
| `标普500ETF`、`沪港深500ETF`、`中证500增强ETF` | 全被算进「中证500」 |
| `沪深300红利ETF`、`沪深300价值ETF` | 全被算进「沪深300」 |

因此规则是 **「指数名紧跟 ETF」才算纯被动跟踪**（正则见 `etfpush/config.py`）：

```python
("中证A500", r"A500ETF"),      ("科创50",  r"(?:科创50|科创板50)ETF"),
("创业板指", r"创业板ETF"),     ("中证500", r"中证500ETF"),
("上证50",  r"上证50ETF"),      ("中证1000", r"中证1000ETF"),
("沪深300", r"沪深300ETF"),
```

红利 / 价值 / 成长 / 增强 / 低波 / 等权这类**策略型**产品不计入对应宽基列（它们仍在 `所有ETF` 里）。

### 数据源：三层通道 + 15 个镜像 + 离线兜底

先说清楚一件事：**「主力净流入」这个指标只有东财一家公开可用**。
腾讯 `ff_` 接口对 ETF 直接返回 `none_match`；新浪的按代码资金流接口对 ETF
会返回**符号相反**的错值（实测 510050：东财 +0.50 亿 / 新浪 −5.33 亿）。
所以"多数据源"只能是**东财的多通道 + 多镜像**，而不是随便找个第三方凑数——
混用会把表格画错，比没有数据更糟。

| 层级 | 通道 | 说明 |
| --- | --- | --- |
| 1 | `ulist.np` 批量 | 主链路，每批 180 只 secid，13 批约 10 秒 |
| 2 | `clist` 分页 | 传统接口，部分网络会被 WAF 掐断 TLS，能通时可用 |
| 3 | akshare `fund_etf_spot_em` | 需 `pip install akshare`，未装则自动跳过 |

上面每个通道都会在 **15 个东财镜像**之间轮换（`push2` / `push2delay` /
`1.push2` … `99.push2`，后端 `svr` 号各不相同）。分批请求时起始镜像也滚动，
压力天然分散；单批失败自动换镜像，连续 3 批全镜像失败则**熔断**，
不再空等超时，把时间让给下一个通道。

**代码表四级降级**（东财挂了也能知道该抓哪些代码）：

1. 天天基金全量基金表（含 A500、科创50 等新品种）
2. 东财 `clist` 板块列表
3. 新浪 ETF 板块列表（约 930 只，会漏新 ETF）
4. 仓库内置 `data/etf_codes.txt`（离线保险，每次成功运行自动回写保持新鲜）

**源的选择不是"有数据就用"，而是"过闸门才用"**：抓完先做完整性校验，
数量不足 400 只、或任一宽基命中不足 3 只，直接判定该源不合格，自动换下一个。

### 完整性闸门

宁可当天不推，也不把残缺数据发到群里。以下情况直接判失败并在群里告警：

- 抓到的 ETF 少于 400 只
- 任一宽基指数命中的 ETF 少于 3 只
- 所有数据源都失败

### 交易日判定

不维护节假日表，直接读**上证指数最新行情的时间戳**：日期不是今天就说明休市，跳过推送。
GitHub 的 cron 只能表达「周一到周五」，所以这个判断是必要的——否则国庆、春节会往群里推昨天的数据。

---

## 本地运行

```bash
git clone <你的仓库地址> && cd etf-feishu-push
pip install -r requirements.txt

# 只看样式（用仓库里的演示数据，不抓数、不推送）
python push.py --demo

# 接口体检：镜像、代码表、兜底源逐个探活（排查"今天怎么没推"先用这个）
python push.py --probe

# 真实抓数 + 出图，但不推送
python push.py --dry-run

# 正式推送（需要先 export 环境变量）
export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
export FEISHU_SECRET="xxx"          # 可选
export FEISHU_APP_ID="cli_xxx"      # 可选，发图片必需
export FEISHU_APP_SECRET="xxx"
python push.py
```

命令参数：

| 参数 | 作用 |
| --- | --- |
| `--demo` | 用 `data/demo_history.json` 出图，验证版式 |
| `--no-fetch` | 不抓数，用 `history.json` 最新一条重推 |
| `--dry-run` | 抓数 + 出图，不推送 |
| `--force` | 忽略「今天非交易日」「今天已推过相同数据」判断 |
| `--card` | 不发图片，改发卡片 |
| `--exclude-today` | 图片里排除今日行 |
| `--days N` | 展示最近 N 个交易日（默认 10） |
| `--probe` | **接口体检**：逐个镜像 / 代码表 / 兜底源探活，不做推送 |

环境变量：`IMAGE_OFF=1` 关闭图片只发卡片；`ALERT_ON_FAIL=0` 关闭失败告警。

> 未配置 `FEISHU_WEBHOOK` 时脚本只做本地预览并正常退出，方便调样式。

## 目录结构

```
.
├── push.py                     # 入口：交易日判定 → 抓数 → 汇总 → 出图 → 推送
├── etfpush/
│   ├── config.py               # 展示列、归类规则、路径、时区
│   ├── sources.py              # 数据源与降级、交易日探测
│   ├── aggregate.py            # 归类汇总、完整性校验
│   ├── render.py               # PIL 渲染表格图片
│   ├── history.py              # data/history.json 读写与去重
│   └── feishu.py               # 签名、图片上传、卡片消息、失败告警
├── data/
│   ├── history.json            # 真实历史（Actions 自动提交）
│   ├── etf_codes.txt           # 离线兜底代码清单（自动回写）
│   └── demo_history.json       # 演示数据（只读）
├── docs/preview.png            # 预览图
└── .github/workflows/daily-push.yml
```

## 改时间 / 改列

- **改推送时间**：编辑 `.github/workflows/daily-push.yml` 里的 `cron: '0 12 * * 1-5'`。cron 用 **UTC**，北京时间减 8 小时，20:00 → `0 12`；30 分 → `30 12`。
- **改展示列**：`etfpush/config.py` 的 `COLUMNS` + `INDEX_PATTERNS`。两处要同步，列名即图片表头。

## 常见问题

**Q：某天没推成功，怎么查是哪一环挂了？**
先跑 `python push.py --probe`，它会逐个报出：交易日探测、15 个镜像、4 个代码表来源、akshare 是否可用。哪个 ❌ 就换哪个——镜像挂了不用管（会自动轮换），代码表全挂才需要关注。

**Q：所有东财接口都挂了会怎样？**
三个通道依次尝试，都失败则**不推送**并往群里发一条失败告警（`ALERT_ON_FAIL=0` 可关）。宁可当天缺一条，也不发残缺或错口径的数据。

**Q：能加新浪 / 腾讯做独立备份吗？**
不能用于这张表。腾讯 `ff_` 不支持 ETF；新浪对 ETF 的资金流与东财口径不一致，实测会出现符号相反的数值。真要独立源，建议接 Tushare/Choice 等付费数据（需自己改 `etfpush/sources.py` 的 `SOURCES`）。

**Q：为什么第一次跑出来的表只有一行？**
数据是逐日累积的。`data/history.json` 每跑一次记一天，跑满 10 个交易日就自动是 10 行。想先看满是 10 行的效果，用 `--demo`。

**Q：Action 显示成功，但没有提交 history.json？**
保持默认分支是 `main`，且仓库 Settings → Actions → General → Workflow permissions 选 **Read and write permissions**。

**Q：飞书只收到卡片，没收到图片？**
没配 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，或自建应用没开 `im:resource` 权限。日志里会写明降级原因。

**Q：金额和东财 App 对不上？**
本表是**按指数类别把多只 ETF 求和**，不是单只 ETF 数据；且已剔除货币类与联接基金。口径写在图片底部。

## 免责声明

数据来自公开行情接口，仅供研究与个人参考，不构成任何投资建议。接口可能变更或限流，请自行评估使用风险。

## License

[MIT](LICENSE)
