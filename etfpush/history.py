# -*- coding: utf-8 -*-
"""历史存档：data/history.json 的读写、去重、取近 N 日。

文件结构：
{
  "records": {
    "2026-11-13": {"metric": "主力净流入", "所有ETF": -23.46, ...},
    ...
  },
  "last_push": {"date": "2026-11-13", "time": "20:03:11", "stats": {...}}
}
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import config


def _read(path) -> dict:
    if not path.exists():
        return {"records": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("records", {})
        return data
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ {path.name} 读取失败，按空历史处理：{exc}")
        return {"records": {}}


def load() -> dict:
    return _read(config.HISTORY_PATH)


def load_demo() -> dict:
    """演示数据：只用于本地调样式，永远不会写回 history.json。"""
    return _read(config.DEMO_PATH)


def save(hist: dict, keep_days: int = 90) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    records = hist.get("records", {})
    if len(records) > keep_days:
        hist["records"] = dict(sorted(records.items())[-keep_days:])
    with open(config.HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def upsert(hist: dict, trade_date: str, stats: dict) -> dict:
    from .aggregate import rounded

    records = hist.setdefault("records", {})
    records[trade_date] = {"metric": config.METRIC, **rounded(stats)}
    hist["records"] = dict(sorted(records.items()))
    return hist


def already_pushed(hist: dict, trade_date: str, stats: dict) -> bool:
    """同日同数据是否已推过（避免手动重跑 / Actions 重试造成刷屏）。"""
    from .aggregate import rounded

    last = hist.get("last_push") or {}
    if last.get("date") != trade_date:
        return False
    prev = {k: round(float(v), 2) for k, v in (last.get("stats") or {}).items()
            if isinstance(v, (int, float))}
    cur = {k: v for k, v in rounded(stats).items() if isinstance(v, (int, float))}
    return prev == cur


def mark_pushed(hist: dict, trade_date: str, stats: dict, stamp: str) -> dict:
    from .aggregate import rounded

    hist["last_push"] = {"date": trade_date, "time": stamp, "stats": rounded(stats)}
    return hist


def recent_rows(hist: dict, today: str, days: int = config.DISPLAY_DAYS) -> list[tuple[str, dict, bool]]:
    """最近 days 条记录，升序返回 [(MM/DD, 记录, 是否今日)]。

    只保留与当前口径（metric）一致的记录：历史里若混入过旧口径
    （比如早版本的「涨跌幅」），画进「亿元」表会直接错数量级。
    """
    records = hist.get("records", {})
    filtered = {d: r for d, r in records.items()
                if not r.get("metric") or r.get("metric") == config.METRIC}
    dates = sorted(filtered.keys())[-days:]
    return [(d[5:].replace("-", "/"), filtered[d], d == today) for d in dates]


def prune_weekend(hist: dict) -> dict:
    """清掉误记在周末的记录（个别情况下手动触发会把日期记成休息日）。"""
    records = hist.get("records", {})
    kept = {}
    for d, rec in records.items():
        try:
            if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
                print(f"  清理周末误记记录 {d}")
                continue
        except ValueError:
            pass
        kept[d] = rec
    hist["records"] = kept
    return hist


def days_between(a: str, b: str) -> int:
    try:
        return abs((datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days)
    except ValueError:
        return 0


def yesterday_of(trade_date: str) -> str:
    d = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=1)
    return d.strftime("%Y-%m-%d")
