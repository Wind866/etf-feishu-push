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
MIN_ETFS_TOTAL = 1200     # 全市场 ETF 数量下限（正常情况下约 1830 只有资金流）
MIN_PER_INDEX = 3         # 没有基准时的保底：每个宽基至少要几只 ETF
MIN_INDEX_RATIO = 0.6     # 有基准时：每个指数至少抓到代码表里 60% 的只数


def sanity_check(info: dict, expected: dict | None = None,
                 expected_total: int | None = None) -> None:
    """expected: {指数: 代码表里该指数的 ETF 只数}；expected_total: 有效候选总数。

    绝对只数不能当标准：全市场上证50 只有 11 只 ETF，抓到 2 只可能是漏了一批，
    但只要抓到 0 只就一定是规则或代码表坏了 —— 用比例判才不会误杀。
    总量同理：固定 1200 的下限在代码表涨到 2100+ 只后形同虚设，
    缺 900 只也能通过，必须再按「占有效候选数的比例」卡一道。
    """
    counts = info.get("counts", {})
    total = counts.get("所有ETF", 0)
    need_total = MIN_ETFS_TOTAL
    if expected_total:
        need_total = max(MIN_ETFS_TOTAL,
                         int(round(expected_total * config.MIN_TOTAL_RATIO)))
    if total < need_total:
        raise RuntimeError(
            f"抓到的 ETF 只有 {total} 只，少于下限 {need_total}"
            + (f"（有效候选 {expected_total} 只 × {config.MIN_TOTAL_RATIO:.0%}）"
               if expected_total else "")
            + "，判定为数据不完整，本次不推送"
        )
    bad = {}
    for col in config.COLUMNS[1:]:
        got = counts.get(col, 0)
        need = MIN_PER_INDEX
        if expected and expected.get(col):
            need = max(MIN_PER_INDEX, round(expected[col] * MIN_INDEX_RATIO))
        if got < need:
            bad[col] = f"{got}/{expected.get(col, '?') if expected else '?'} 只，需 ≥{need}"
    if bad:
        raise RuntimeError(
            f"以下指数命中的 ETF 数量异常：{bad}，"
            "可能是漏抓批次或代码表不全，本次不推送"
        )


def evaluate(items: list[tuple[str, float]], expected: dict | None = None,
             expected_total: int | None = None) -> tuple[dict, dict]:
    """聚合 + 完整性校验，返回 (stats, info)；不合格直接抛异常。"""
    stats, info = aggregate(items)
    sanity_check(info, expected, expected_total)
    return stats, info
