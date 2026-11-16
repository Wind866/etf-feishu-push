#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 各指数主力资金净流入 → 飞书（图片 / 卡片）

一次运行的完整链路：
    交易日判定 → 抓取全市场 ETF 主力净流入 → 按指数归类汇总（亿元）
    → 落库 data/history.json → 渲染近 10 个交易日对照表 → 推送飞书

用法：
    python push.py                    # 抓取 + 推送（Actions 里就是这一条）
    python push.py --demo             # 用 demo_history.json 出图，看样式，不推送
    python push.py --no-fetch         # 不抓数，用存档最新数据重推
    python push.py --dry-run          # 抓数 + 出图，但不推送
    python push.py --force            # 忽略「今日非交易日」与「同日已推」判断
    python push.py --card             # 不推图片，改推卡片
    python push.py --exclude-today    # 图片不含今天（只发历史）

关于「多数据源」的说明（重要，别被三条通道的名字骗了）
--------------------------------------------------------
`sources.SOURCES` 里的三条通道（东财·批量 / 东财·分页 / akshare·东财）以及
15 个镜像，**全部指向东方财富同一家**：三个 fetch_* 函数打的是同一套
push2/push2his 接口，镜像只是同一集群的不同 svr 域名。所以「东财整体封 IP」
（你们自己的排查记录里写过「5 分钟以上所有接口不可用」）时，三条会一起失效。

这是指标本身的客观限制：ETF 主力净流入只有东财提供，换别家等于换口径。
因此这里的降级路径不是「再找一个源」，而是**T+1 回补**：当天全挂时本次
照实失败并告警，第二天的日线接口（fetch_history）能把这一天的数补回来，
10 日对照表不会永久缺一格 —— 见 `_auto_backfill()`。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timedelta

from etfpush import aggregate, config, feishu, history, render, sources


def _now():
    return datetime.now(config.CN_TZ)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ETF 各指数主力净流入 → 飞书",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--demo", action="store_true", help="用演示数据出图（不抓数、不写库、不推送）")
    p.add_argument("--no-fetch", action="store_true", help="不抓数，用 history.json 最新一条")
    p.add_argument("--dry-run", action="store_true", help="抓数出图但不推送")
    p.add_argument("--force", action="store_true", help="忽略交易日与重复推送判断")
    p.add_argument("--card", action="store_true", help="只发卡片消息，不发图片")
    p.add_argument("--exclude-today", action="store_true", help="图片中排除今日行")
    p.add_argument("--days", type=int, default=config.DISPLAY_DAYS, help="展示最近 N 个交易日")
    p.add_argument("--probe", action="store_true", help="只做接口体检：镜像、代码表、兜底源能否连通")
    return p.parse_args(argv)


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格（中文占 2 格），让体检结果对齐。"""
    shown = sum(2 if ord(ch) > 127 else 1 for ch in text)
    return text + " " * max(width - shown, 1)


def _run_probe() -> int:
    print("=== 接口体检（不做推送）===")
    rows = sources.probe()
    ok = sum(1 for _, good, _ in rows if good)
    for name, good, detail in rows:
        print(f"  {'✅' if good else '❌'} {_pad(name, 30)}{detail}")
    print(f"\n{ok}/{len(rows)} 项可用")
    return 0 if ok else 1


def _print_table(hist: dict, trade_date: str, days: int) -> None:
    rows = history.recent_rows(hist, trade_date, days)
    if not rows:
        return
    head = f"{'日期':<8}" + "".join(f"{c:>12}" for c in config.COLUMNS)
    print("\n" + head)
    print("-" * len(head))
    for date_txt, record, is_today in rows:
        cells = []
        for col in config.COLUMNS:
            val = record.get(col)
            cells.append(f"{float(val):>12.2f}" if isinstance(val, (int, float)) else f"{'-':>12}")
        print(f"{date_txt + ('*' if is_today else ' '):<8}" + "".join(cells))
    print()


def _backfill_reason(hist: dict, trade_date: str) -> str | None:
    """需要回补历史时返回原因（写进日志、供测试断言）；历史完整时返回 None。

    原版只看「近 20 天记录数 < DISPLAY_DAYS」，只覆盖新仓库冷启动：
    中途某天被东财限流漏推时记录数仍是 10，函数直接返回 —— 缺的那一格
    永远补不上，与文档承诺的「10 日对照表不会永久缺一格」不符。

    这里加一道**精确缺口检测**：用上证指数日 K 当交易日历（每个交易日
    必有 K 线，周末/节假日天然缺席，见 sources.trade_calendar），找出
    近 20 个自然日内「真实交易日却没进 history.json」的日子。
      * 节假日不会误报（不在日历里）；
      * 真漏推的日子每天重试直到补上，正是期望行为；
      * 日历探测失败时保守返回 None（回补是锦上添花，不能误触发）。
    """
    records = sorted(hist.get("records") or {})
    try:
        since = (datetime.strptime(trade_date, "%Y-%m-%d")
                 - timedelta(days=20)).strftime("%Y-%m-%d")
    except ValueError:
        return None
    have = sum(1 for d in records if d >= since)
    if have < config.DISPLAY_DAYS:
        return (f"近 20 天只有 {have}/{config.DISPLAY_DAYS} 个交易日的历史"
                "（冷启动或长期缺数）")
    missing = [d for d in sources.trade_calendar(lmt=40)
               if since <= d <= trade_date and d not in set(records)]
    if missing:
        return f"近 20 天内有真实交易日缺记录：{missing}"
    return None


def _auto_backfill(hist: dict, trade_date: str) -> None:
    """历史不足或有缺口时，顺手补一轮（**推送成功之后**才调用）。

    新仓库上线后 history.json 只有当天一条：10 日对照表要干等两周才长出来；
    中途某天被东财限流漏推，那张表也会永久缺一格。指望人记得去点
    workflow_dispatch(mode=backfill) 并不现实，所以这里自动补，触发条件
    （满足其一，见 _backfill_reason）：

      * 近 20 个自然日内的记录数 < DISPLAY_DAYS（刚开始积累、长期缺数）；
      * 日历检测到近 20 天内有真实交易日缺记录（单日漏推的精确修复）。

    补齐即停：历史完整时这个函数什么都不做。复用 backfill.py 的断点
    缓存与完整性闸门，补出来的数与当日推送同口径。必须放在「推送成功、
    推送状态已写」之后：补历史是锦上添花，不能因为它失败就把当天的
    推送判成失败 —— 所有异常都在这里吞掉并记录。
    """
    if os.environ.get("AUTO_BACKFILL", "1").strip() == "0":
        print("ℹ️ AUTO_BACKFILL=0，跳过多日历史自动回补")
        return
    reason = _backfill_reason(hist, trade_date)
    if not reason:
        return
    print(f"🧩 自动回补历史：{reason}")
    try:
        import backfill  # noqa: PLC0415 - 日常推送不需要它，延迟导入
        # 原来只补最近 10 个交易日：缺口落在第 11~14 个交易日时永远追不上，
        # 会变成每天空跑。--days 20 覆盖整个 20 个自然日检测窗口。
        backfill.main(["--days", "20"])
    except SystemExit as exc:
        print(f"⚠️ 自动回补提前结束（exit={exc.code}），历史可能仍不完整")
    except Exception as exc:  # noqa: BLE001 - 绝不能影响已完成的推送
        print(f"⚠️ 自动回补失败（不影响本次推送）：{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started = _now()
    print(f"=== ETF 资金推送 · {started:%Y-%m-%d %H:%M:%S} (UTC+8) ===")

    if args.probe:
        return _run_probe()

    # ---------- 1. 取数 ----------
    if args.demo:
        hist = history.load_demo()
        if not hist.get("records"):
            print("❌ data/demo_history.json 为空，无法演示")
            return 1
        trade_date = max(hist["records"].keys())
        source = "演示数据"
        print(f"演示模式：使用 {trade_date} 起的 {len(hist['records'])} 天样例数据")
    elif args.no_fetch:
        hist = history.load()
        if not hist.get("records"):
            print("❌ history.json 无数据，--no-fetch 不可用")
            return 1
        trade_date = max(hist["records"].keys())
        source = "history.json 存档"
        print(f"复用存档：{trade_date}")
    else:
        hist = history.load()          # 提前加载：交易日/推送判重都要读它
        today = started.date()
        data_date = today
        if not args.force:
            ok, data_date, reason = sources.trading_session(today)
            if not ok:
                print(f"⏭  跳过：{reason}")
                return 0
            print(f"交易日检查通过：{reason}")
            if data_date != today:
                print(f"  ℹ️ 数据按行情日期 {data_date} 记账（运行日 {today}）")
            if history.recently_pushed(hist, data_date.strftime("%Y-%m-%d"), started):
                print(f"⏭  {data_date} 近期已推送过，跳过（多档 cron 幂等）")
                return 0

        # 数据源选择带质量校验：不合格的源自动跳过，换下一个。
        # 基准同时包含各指数只数与「有效候选总数」，闸门按比例判，
        # 不会再出现「代码表 2100 只、只抓到 1300 只也算合格」的情况。
        baseline = sources.universe_stats()
        validate = lambda its: aggregate.evaluate(  # noqa: E731
            its, baseline.get("indices"), baseline.get("total"))
        items, source = sources.fetch_flow(validate=validate)
        stats, info = validate(items)
        trade_date = data_date.strftime("%Y-%m-%d")
        print(f"统计完成（{info['total']} 个标的，剔除货币类 {info['cash_excluded']} 只、"
              f"非ETF {info['non_etf']} 只，未归类 {info['unmapped']} 只）")
        print("  " + "  ".join(f"{k}={v:.2f}亿" for k, v in stats.items()))

        if not args.force and history.already_pushed(hist, trade_date, stats):
            print(f"⏭  今天（{trade_date}）已推送过相同数据，跳过；如需重推加 --force")
            return 0

        hist = history.upsert(hist, trade_date, stats)
        hist = history.prune_weekend(hist)
        if not args.dry_run:
            history.save(hist)
            print(f"💾 已写入 {config.HISTORY_PATH.relative_to(config.ROOT)}")

    _print_table(hist, trade_date, args.days)

    # ---------- 2. 出图 ----------
    png = None
    try:
        png = render.render_snapshot(
            hist, trade_date, source,
            days=args.days,
            exclude_today=args.exclude_today,
            demo=args.demo,
        )
        print(f"🖼 图片已生成：{png}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 出图失败（降级为卡片）：{type(exc).__name__}: {exc}")

    # ---------- 3. 推送 ----------
    if args.demo:
        print("ℹ️ 演示模式：只出图，不推送")
        return 0
    if args.dry_run:
        print("ℹ️ dry-run：跳过推送")
        return 0
    if not feishu.webhook():
        print("ℹ️ 未配置 FEISHU_WEBHOOK，本次只做本地预览")
        return 0

    rows = history.recent_rows(hist, trade_date, args.days)
    sent = False
    if png and not args.card and os.environ.get("IMAGE_OFF") != "1":
        try:
            sent = feishu.send_image(png)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 图片消息失败，降级为卡片：{type(exc).__name__}: {exc}")
    if not sent:
        sent = feishu.send_card(rows, trade_date)

    if not sent:
        raise RuntimeError("图片与卡片均未发送成功")

    # ---------- 4. 记录推送状态 ----------
    if not (args.demo or args.no_fetch or args.dry_run):
        if trade_date in hist.get("records", {}):
            stats = hist["records"][trade_date]
            hist = history.mark_pushed(hist, trade_date, stats, started.strftime("%H:%M:%S"))
            history.save(hist)
            print("📝 已记录推送状态（同日重复运行会自动跳过）")
        else:
            # --force 在周末/节假日强推时，该交易日记录会被 prune_weekend 清理，
            # 这里不应再按 trade_date 取记录，否则 KeyError 把整个任务判成失败、
            # 还误发一条失败告警。图片/卡片已经发出去了，只是不写推送台账。
            print(f"ℹ️ {trade_date} 未写入历史（被周末清理或未通过校验），跳过推送状态记录")

    # ---------- 4.5 历史自愈（可选，失败不影响本次推送） ----------
    # 放在最后：此时飞书已经收到消息、推送状态也已落库，补历史出任何问题
    # 都只是「表里少一天」，不该把整次运行判成失败。
    if not (args.demo or args.no_fetch or args.dry_run):
        _auto_backfill(history.load(), trade_date)

    print(f"=== 完成，用时 {(_now() - started).seconds}s ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"🛑 任务失败：{exc}")
        traceback.print_exc()
        feishu.notify_failure(f"{type(exc).__name__}: {exc}")
        sys.exit(1)
