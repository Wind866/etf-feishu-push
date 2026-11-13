# -*- coding: utf-8 -*-
"""回补历史：把最近 N 个交易日的全市场 ETF 主力净流入补进 data/history.json。

为什么要单独一个脚本：日常推送只用得上「当日快照」（ulist 的 f62），
历史数据得逐只去取「资金流向-日线」接口。一只 ETF 一次请求就能拿到
一整段历史，所以 1800 只 ETF 是 1800 次请求，不是 1800×N 次。

归类和剔重用的是和线上完全相同的 aggregate 规则，保证补出来的历史
和以后每天推的数是同一口径（否则曲线会在拼接处跳变）。

用法：
    python backfill.py                # 补最近 10 个交易日（已有的日期跳过）
    python backfill.py --days 20      # 补 20 天
    python backfill.py --force        # 覆盖已有日期（比如当日推送数据有误）
"""

from __future__ import annotations

import argparse
import sys

from etfpush import aggregate, config, history, sources


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="回补最近 N 个交易日的 ETF 资金流历史")
    p.add_argument("--days", type=int, default=config.DISPLAY_DAYS,
                   help=f"回补最近 N 个交易日（默认 {config.DISPLAY_DAYS}）")
    p.add_argument("--workers", type=int, default=3,
                   help="并发数（默认 3；日线接口频控很严，调到 6 以上容易被封）")
    p.add_argument("--pace", type=float, default=0.4,
                   help="每只之间的间隔秒数（默认 0.4）")
    p.add_argument("--refresh", action="store_true",
                   help="忽略断点缓存，全部重取（缓存过期或想换口径时用）")
    p.add_argument("--force", action="store_true",
                   help="覆盖已存在的日期（默认保留，避免改掉已经推过的数）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    print(f"=== 历史回补 · 目标最近 {args.days} 个交易日 ===")

    # 多取一些日子再截断：长假 / 停牌会让实际交易日比日历天数少
    expect = sources.expected_counts()
    flows = sources.fetch_history(days=max(args.days * 2, 24), workers=args.workers,
                                  pace=args.pace, refresh=args.refresh)
    dates = sorted(flows)[-args.days:]

    hist = history.load()
    written, skipped, bad = 0, 0, 0
    for d in dates:
        if d in hist.get("records", {}) and not args.force:
            print(f"⏭  {d} 已有记录，跳过（--force 可覆盖）")
            skipped += 1
            continue
        try:
            stats, info = aggregate.evaluate(flows[d], expect)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ {d} 数据不完整，跳过：{exc}")
            bad += 1
            continue
        hist = history.upsert(hist, d, stats)
        total = info["counts"]["所有ETF"]
        print(f"✅ {d} 写入：{total} 只 ETF，所有ETF {stats['所有ETF']:+.2f} 亿")
        written += 1

    hist = history.prune_weekend(hist)
    history.save(hist)
    print(f"\n完成：写入 {written} 天，跳过 {skipped} 天，数据不合格 {bad} 天")
    print(f"存档：{config.HISTORY_PATH.relative_to(config.ROOT)}（共 "
          f"{len(hist.get('records', {}))} 天）")

    rows = history.recent_rows(hist, today=max(hist.get("records", {}) or {"": {}}))
    if rows:
        print("\n已存入的近 %d 日：" % len(rows))
        header = f"{'日期':<10}" + "".join(f"{c:>12}" for c in config.COLUMNS)
        print(header)
        print("-" * (10 + 12 * len(config.COLUMNS)))
        for label, rec, is_today in rows:
            vals = "".join(f"{rec.get(c, 0):>12.2f}" for c in config.COLUMNS)
            print(f"{label + ('*' if is_today else ''):<10}{vals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
