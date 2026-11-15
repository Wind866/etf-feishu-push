"""卡片出图的冒烟测试：锁住 Pillow 升级时的兼容性。

CI 只跑单测，而 ``etfpush/render.py`` 之前没有任何测试覆盖 —— 换句话说
Pillow 从 10.x 跳到 13.x 时，即使 ``ImageFont.truetype`` 或 ``ImageDraw``
的签名变了，测试也会全绿，直到某个交易日真去出图才在推送步骤炸掉
（卡片没发出去，但 history 已经写了 last_push，当天就补不回来）。

这里不比对像素，只断言「能从 history 走通到一张结构合法的 PNG」：
``recent_rows`` 造行 → ``render_table`` 出图 → 检查文件头/尺寸/可解码性。
字体在无 DejaVu 的机器上会退回 ``ImageFont.load_default()``，因此不依赖具体字体。
"""
from __future__ import annotations

import os
import tempfile
import unittest

try:
    from PIL import Image
except ImportError:  # 本地没装 Pillow 时跳过，CI 会装 requirements.txt
    Image = None

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 与 config.COLUMNS 同构；数值单位是亿元，正负都有，覆盖红/绿两种着色分支
RECORDS = {
    "2026-11-11": {"metric": "主力净流入", "所有ETF": -12.5, "沪深300": 3.2,
                   "中证1000": 0.8, "上证50": -1.1, "中证500": 0.0,
                   "创业板指": -4.4, "科创50": 1.9, "中证A500": 2.2},
    "2026-11-12": {"metric": "主力净流入", "所有ETF": 8.1, "沪深300": -0.4,
                   "中证1000": -2.7, "上证50": 0.5, "中证500": 1.3,
                   "创业板指": 3.6, "科创50": -1.2, "中证A500": 0.9},
    # 旧口径记录：recent_rows 必须按 metric 过滤掉，不能画进「亿元」表
    "2026-11-10": {"metric": "涨跌幅", "所有ETF": 0.01},
    "2026-11-13": {"metric": "主力净流入", "所有ETF": -74.44, "沪深300": -6.25,
                   "中证1000": 1.43, "上证50": 0.52, "中证500": 0.1,
                   "创业板指": -29.1, "科创50": -7.75, "中证A500": 1.56},
}


@unittest.skipIf(Image is None, "未安装 Pillow，跳过出图测试")
class TestRenderTable(unittest.TestCase):
    def test_history_to_png(self):
        from etfpush import config, history, render

        hist = {"records": RECORDS}
        rows = history.recent_rows(hist, today="2026-11-13", days=5)
        # 只留 3 条同口径记录，最后一条被标「今日」
        self.assertEqual([r[0] for r in rows], ["11/11", "11/12", "11/13"])
        self.assertEqual([r[2] for r in rows], [False, False, True])

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "card.png")
            out = render.render_table(
                rows, config.COLUMNS,
                "ETF 各指数主力净流入 · 近 3 个交易日（净流出）",
                "2026-11-11 - 2026-11-13 · 主力净流入（亿元）",
                ["数据来源：东方财富 · 主力净流入口径", "仅供参考，不构成投资建议"],
                path,
            )
            # 返回的就是落盘路径，调用方拿它去发图，不能悄悄换个名字
            self.assertEqual(os.path.abspath(out), os.path.abspath(path))
            self.assertTrue(os.path.exists(path), "render_table 没有生成文件")

            with open(path, "rb") as fh:
                self.assertEqual(fh.read(8), PNG_MAGIC, "产物不是 PNG")

            with Image.open(path) as img:
                img.load()  # 强制解码，截断/损坏的图会在这里报错
                width, height = img.size
                self.assertEqual(img.format, "PNG")
                # 只断言「是一张真正的表格图」，不锁死具体像素：
                # 改字号/列宽/换字体都会让尺寸变化，但不会掉到几百字节。
                self.assertGreater(width, 400, f"图太窄（{width}px），可能没画出表格")
                self.assertGreater(height, 200, f"图太矮（{height}px），可能没画出表格")

            self.assertGreater(os.path.getsize(path), 5000, "PNG 体积异常，疑似空图")


if __name__ == "__main__":
    unittest.main()
