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
import os
import shutil
from datetime import datetime, timedelta

from . import config


def _read(path) -> dict:
    if not path.exists():
        return {"records": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("history.json 顶层不是对象")
        data.setdefault("records", {})       # 兼容早期没有 records 键的存档
        if not isinstance(data["records"], dict):
            raise ValueError("records 不是对象")
        return data
    except Exception as exc:  # noqa: BLE001
        # 原实现会静默按空历史处理，随后 save() 直接覆写，坏文件里的历史就永久没了。
        # 先留一份带时间戳的备份，再让调用方以空历史继续。
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
        try:
            shutil.copy2(path, backup)
            print(f"⚠️ {path.name} 读取失败，已备份到 {backup.name}：{exc}")
        except Exception as copy_exc:  # noqa: BLE001
            print(f"⚠️ {path.name} 读取失败且备份失败：{exc} / {copy_exc}")
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
    # 原子写：Actions 被超时杀掉 / 磁盘写一半时，直接 open("w") 会留下截断的
    # JSON，下次启动按空历史处理，整段积累的历史就没了。
    tmp = config.HISTORY_PATH.with_name(config.HISTORY_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, config.HISTORY_PATH)


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


def recently_pushed(hist: dict, trade_date: str, now=None,
                    hours: int = config.SKIP_WINDOW_HOURS) -> bool:
    """同一「数据日期」在最近 hours 小时内是否已推送过。

    多档 cron 兜底 20/21/22 点都会触发，靠这个在抓数之前就退出，
    既不重复刷屏，也不浪费一次全市场抓取。按 last_push 的日期+时间判，
    --force 由调用方跳过本检查。时间解析不了时保守返回 True（宁可不推）。
    """
    last = hist.get("last_push") or {}
    if last.get("date") != trade_date:
        return False
    try:
        stamp = datetime.strptime(
            f"{trade_date} {last.get('time') or '00:00:00'}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=config.CN_TZ)
        now = now or datetime.now(config.CN_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=config.CN_TZ)
        delta = (now - stamp).total_seconds()
        return 0 <= delta < hours * 3600
    except Exception:  # noqa: BLE001
        return True


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
