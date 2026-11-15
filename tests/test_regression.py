"""行为回归测试：只针对这次修掉的正确性/稳定性缺陷，不联网、不碰 Secret。

原 CI 只做静态字符串断言（"workflow 里有没有 if: success()"），
接口改了、日期错了、闸门松了都测不出来，所以补这一层。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from etfpush import aggregate, config, history, sources

ROOT = Path(__file__).resolve().parent.parent


class TestTradingSession(unittest.TestCase):
    """数据日期必须来自行情，而不是运行时刻。"""

    def test_delayed_run_uses_quote_date(self):
        # cron 延迟到次日凌晨：运行日 11-16，最新行情 11-13
        with patch.object(sources, "latest_trade_date", return_value=date(2026, 11, 13)):
            ok, data_date, reason = sources.trading_session(date(2026, 11, 16))
        self.assertTrue(ok)
        self.assertEqual(data_date, date(2026, 11, 13))
        self.assertIn("行情日期记账", reason)

    def test_probe_failure_fails_closed(self):
        with patch.object(sources, "latest_trade_date", return_value=None):
            ok, _d, reason = sources.trading_session(date(2026, 11, 16))
        self.assertFalse(ok)
        self.assertIn("跳过", reason)

    def test_implausible_timestamps_rejected(self):
        today = date(2026, 11, 16)
        self.assertFalse(sources._plausible_quote_date(date(1970, 1, 1), today))
        self.assertFalse(sources._plausible_quote_date(date(2030, 1, 1), today))
        self.assertTrue(sources._plausible_quote_date(date(2026, 11, 13), today))

    def test_weekend_is_not_a_hard_block(self):
        # 周末跑时数据日期是周五；是否推送交给 history 按日期判重，
        # 这样节假日后漏推的那天仍有机会补上。
        with patch.object(sources, "latest_trade_date", return_value=date(2026, 11, 13)):
            ok, data_date, _ = sources.trading_session(date(2026, 11, 15))
        self.assertTrue(ok)
        self.assertEqual(data_date, date(2026, 11, 13))


class TestQualityGate(unittest.TestCase):
    def test_relative_total_gate_blocks_partial_fetch(self):
        names = {"沪深300": "沪深300ETF", "中证1000": "中证1000ETF",
                 "上证50": "上证50ETF", "中证500": "中证500ETF",
                 "创业板指": "创业板ETF", "科创50": "科创50ETF",
                 "中证A500": "A500ETF"}
        items = [("随机ETF%d" % i, 1e6) for i in range(1300)]
        for name in names.values():
            items += [(name + str(i), 1e6) for i in range(20)]
        _stats, info = aggregate.aggregate(items)
        with self.assertRaisesRegex(RuntimeError, "少于下限"):
            aggregate.evaluate(items, expected=None, expected_total=2000)

    def test_full_coverage_passes(self):
        names = {"沪深300": "沪深300ETF", "中证1000": "中证1000ETF",
                 "上证50": "上证50ETF", "中证500": "中证500ETF",
                 "创业板指": "创业板ETF", "科创50": "科创50ETF",
                 "中证A500": "A500ETF"}
        items = [("随机ETF%d" % i, 1e6) for i in range(1900)]
        for name in names.values():
            items += [(name + str(i), 1e6) for i in range(20)]
        stats, info = aggregate.evaluate(items, expected=None, expected_total=2000)
        self.assertIn("所有ETF", stats)


class TestUniverseFilter(unittest.TestCase):
    def test_offmarket_funds_are_rejected(self):
        for name in ("东方红睿玺三年持有混合A", "南方原油A", "银华明择多策略定期开放混合",
                     "汇添富收益快钱货币B", "华夏上证50ETF联接A", "鹏华国防指数(LOF)A"):
            with self.subTest(name=name):
                self.assertFalse(sources._looks_like_etf(name))

    def test_real_etfs_are_kept(self):
        for name in ("华泰柏瑞沪深300ETF", "300ETF", "中证A500ETF基金",
                     "科创50ETF", "国债政金债ETF招商", "纳指ETF"):
            with self.subTest(name=name):
                self.assertTrue(sources._looks_like_etf(name))


class TestHistoryDurability(unittest.TestCase):
    def test_corrupt_file_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text('{"records": {broken', encoding="utf-8")
            with patch.object(config, "HISTORY_PATH", path):
                data = history._read(path)
            self.assertEqual(data, {"records": {}})
            backups = list(Path(tmp).glob("history.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("broken", backups[0].read_text(encoding="utf-8"))

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            with patch.object(config, "HISTORY_PATH", path):
                history.save({"records": {"2026-11-13": {"所有ETF": -1.23}}})
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["records"]
                                 ["2026-11-13"]["所有ETF"], -1.23)
                self.assertFalse(path.with_name("history.json.tmp").exists())

    def test_recently_pushed_idempotency(self):
        now = datetime(2026, 11, 13, 21, 0, 0, tzinfo=config.CN_TZ)
        hist = {"last_push": {"date": "2026-11-13", "time": "20:00:00", "stats": {}}}
        self.assertTrue(history.recently_pushed(hist, "2026-11-13", now))
        self.assertFalse(history.recently_pushed(hist, "2026-11-12", now))
        later = now + timedelta(hours=5)
        self.assertFalse(history.recently_pushed(hist, "2026-11-13", later))


class TestBackfillCache(unittest.TestCase):
    def test_stale_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            cache.write_text(json.dumps({"510300": {"2026-11-12": 1.0}}), encoding="utf-8")
            universe = [{"code": "510300", "secid": "1.510300", "name": "沪深300ETF"}]
            with patch.object(sources, "get_universe", return_value=universe), \
                 patch.object(sources, "_daykline", return_value={"2026-11-13": 9.0}) as daykline:
                out = sources.fetch_history(days=24, pace=0, cache_path=cache,
                                            max_passes=1,
                                            stale_date=date(2026, 11, 13))
            self.assertEqual(daykline.call_count, 1, "缓存过期却没重取日线")
            self.assertIn("2026-11-13", out)

    def test_fresh_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            cache.write_text(json.dumps({"510300": {"2026-11-13": 1.0}}), encoding="utf-8")
            universe = [{"code": "510300", "secid": "1.510300", "name": "沪深300ETF"}]
            with patch.object(sources, "get_universe", return_value=universe), \
                 patch.object(sources, "_daykline", return_value={}) as daykline:
                sources.fetch_history(days=24, pace=0, cache_path=cache,
                                      max_passes=1, stale_date=date(2026, 11, 13))
            self.assertEqual(daykline.call_count, 0)


class TestWorkflowContract(unittest.TestCase):
    def test_multiple_cron_fallbacks(self):
        src = (ROOT / ".github/workflows/daily-push.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("- cron:"), 3)

    def test_alert_uses_signed_sender(self):
        src = (ROOT / ".github/workflows/daily-push.yml").read_text(encoding="utf-8")
        self.assertIn("feishu.send", src)
        self.assertNotIn('curl -sS -X POST "$HOOK"', src)


if __name__ == "__main__":
    unittest.main()
