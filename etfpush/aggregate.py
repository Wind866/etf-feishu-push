# -*- coding: utf-8 -*-
"""归类与聚合：把逐只 ETF 的主力净流入汇总到 8 个展示列。"""

from __future__ import annotations

import re

from . import config

_PATTERNS = [(col, re.compile(pat)) for col, pat in config.INDEX_PATTERNS]


def classify(name: str) -> str:
    """ETF 名称 → 展示列名；不属于任何一个宽基指数时返回空串。"""
    if not name:
        return ""
    name = str(name).strip()
    upper = name.upper()
    for column, pattern in _PATTERNS:
        if pattern.search(upper):
            return column
    for prefix, column in config.PREFIX_ALIASES.items():
        if upper.startswith(prefix):
            return column
    return ""


def is_cash_like(name: str) -> bool:
    """货币 / 现金管理类 ETF（资金流无分析意义）。"""
    return any(k in str(name) for k in config.CASH_KEYWORDS)


def is_non_etf(name: str) -> bool:
    """代码段在 ETF 区间、但本身不是 ETF 的品种（联接基金、LOF、分级子份额）。"""
    upper = str(name).upper()
    return any(k in upper for k in config.NON_ETF_KEYWORDS)


def aggregate(items: list[tuple[str, float]]) -> tuple[dict, dict]:
    """items: [(ETF 名称, 主力净流入元)] → ({列名: 亿元}, 统计信息)。"""
    stats = {col: 0.0 for col in config.COLUMNS}
    counts = {col: 0 for col in config.COLUMNS}
    cash, no_data, unmapped, non_etf = 0, 0, 0, 0

    for name, value in items:
        if value is None:
            no_data += 1
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            no_data += 1
            continue
        if val != val:            # NaN
            no_data += 1
            continue
        if is_non_etf(name):
            non_etf += 1
            continue
        if is_cash_like(name):
            cash += 1
            continue
        yi = val / 1e8            # 元 → 亿元
        stats["所有ETF"] += yi
        counts["所有ETF"] += 1
        column = classify(name)
        if column:
            stats[column] += yi
            counts[column] += 1
        else:
            unmapped += 1

    info = {"total": len(items), "cash_excluded": cash, "non_etf": non_etf,
            "no_data": no_data, "unmapped": unmapped, "counts": counts}
    return stats, info


def rounded(stats: dict) -> dict:
    """落库前统一保留 2 位，避免浮点尾差让「同一天重复推送」判断失效。"""
    out = {}
    for col in config.COLUMNS:
        val = stats.get(col)
        out[col] = round(float(val), 2) if isinstance(val, (int, float)) and val == val else val
    return out


# 数据完整性闸门：宁可这一次不推，也不要把残缺数据发到群里
MIN_ETFS_TOTAL = 400      # 全市场 ETF 数量下限（当前约 1600 只有资金流）
MIN_PER_INDEX = 3         # 每个宽基指数至少要有几只 ETF 命中


def sanity_check(info: dict) -> None:
    counts = info.get("counts", {})
    total = counts.get("所有ETF", 0)
    if total < MIN_ETFS_TOTAL:
        raise RuntimeError(
            f"抓到的 ETF 只有 {total} 只，少于下限 {MIN_ETFS_TOTAL}，"
            "判定为数据不完整，本次不推送"
        )
    missing = {c: counts.get(c, 0) for c in config.COLUMNS[1:] if counts.get(c, 0) < MIN_PER_INDEX}
    if missing:
        raise RuntimeError(
            f"以下指数命中的 ETF 数量异常（<{MIN_PER_INDEX} 只）：{missing}，"
            "可能是代码表不全或分类规则需要更新，本次不推送"
        )
