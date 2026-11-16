# -*- coding: utf-8 -*-
"""把统计表渲染成 PNG（白底 / 浅蓝表头 / 红涨绿跌 / 今日行高亮）。

设计目标是"飞书里点开能看清"：不做花哨渐变，纯表格 + 大字号，
手机上双指放大后每一格数字依然清晰。
"""

from __future__ import annotations

import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from . import config

# ---------------- 尺寸 ----------------
W_DATE = 195          # 日期列宽
W_COL = 160           # 数据列宽（"23.45亿" 最宽约 100px，留足呼吸感）
PAD_X = 44            # 左右留白
H_TITLE = 122         # 标题区
H_HEAD = 76           # 表头
H_ROW = 80            # 数据行
H_FOOT = 104          # 底部注释

# ---------------- 配色 ----------------
C_BG = (255, 255, 255)
C_TITLE = (26, 30, 36)
C_SUB = (138, 147, 159)
C_HEAD_BG = (231, 240, 252)
C_HEAD_LINE = (208, 222, 240)
C_HEAD_TX = (37, 56, 82)
C_LINE = (231, 237, 244)
C_BORDER = (210, 221, 234)
C_RED = (211, 47, 47)          # 净流入
C_GREEN = (18, 145, 82)        # 净流出
C_ZERO = (78, 88, 100)
C_ROW_ALT = (250, 251, 253)
C_TODAY_BG = (255, 248, 230)
C_TODAY_TAG = (198, 40, 40)
C_FOOT = (146, 154, 165)

# ---------------- 字体（按平台挑第一个存在的） ----------------
FONT_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
FONT_REGULAR = [
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _cache:
        return _cache[key]
    for path in (FONT_BOLD if bold else FONT_REGULAR):
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size, index=0)
                _cache[key] = font
                return font
            except Exception:  # noqa: BLE001
                continue
    print("⚠️ 未找到中文字体，图片里中文可能显示为方块（Linux 上请安装 fonts-noto-cjk）")
    font = ImageFont.load_default()
    _cache[key] = font
    return font


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _draw_center(draw, text, font, box_x, box_w, box_y, box_h, fill):
    """在 (box_x, box_y, w, h) 区域内水平垂直居中绘制文本。"""
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text((box_x + (box_w - w) / 2, box_y + (box_h - h) / 2 - box[1]),
              text, font=font, fill=fill)


def _fmt(value) -> tuple[str, tuple]:
    """数值 → (显示文本, 颜色)；None/NaN 统一显示 '-'。"""
    if value is None:
        return "-", C_ZERO
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "-", C_ZERO
    if n != n:                       # NaN
        return "-", C_ZERO
    if n == 0:
        return "0.00亿", C_ZERO
    return f"{n:,.2f}亿", (C_RED if n > 0 else C_GREEN)


def render_table(rows: list[tuple[str, dict, bool]], columns: list[str],
                 title: str, subtitle: str, footnotes: list[str], path: str,
                 today_label: str | None = "今日") -> str:
    """rows: [(日期 MM/DD, {列名: 数值}, 是否今日)]，自上而下按时间升序。

    出图按 config.RENDER_SCALE 超采样（默认 2x）：所有尺寸与字号成倍放大后
    直接保存高分辨率 PNG。飞书端按屏宽缩放显示，2x 源缩出来的文字边缘
    明显比 1x 源锐利；图内布局比例与 1x 完全一致。
    """
    s = max(1, int(getattr(config, "RENDER_SCALE", 1)))
    w_date, w_col, pad_x = W_DATE * s, W_COL * s, PAD_X * s
    h_title, h_head, h_row, h_foot = H_TITLE * s, H_HEAD * s, H_ROW * s, H_FOOT * s

    width = pad_x * 2 + w_date + w_col * len(columns)
    height = h_title + h_head + h_row * len(rows) + h_foot
    img = Image.new("RGB", (width, height), C_BG)
    draw = ImageDraw.Draw(img)

    # ---------- 标题 ----------
    draw.text((pad_x, 30 * s), title, font=_font(44 * s, True), fill=C_TITLE)
    draw.text((pad_x, 86 * s), subtitle, font=_font(25 * s), fill=C_SUB)

    # ---------- 表头 ----------
    top, right = h_title, width - pad_x
    draw.rectangle([pad_x, top, right, top + h_head], fill=C_HEAD_BG)
    f_head = _font(32 * s, True)
    box = draw.textbbox((0, 0), "日期", font=f_head)
    draw.text((pad_x + 22 * s, top + (h_head - (box[3] - box[1])) / 2 - box[1]),
              "日期", font=f_head, fill=C_HEAD_TX)
    for i, col in enumerate(columns):
        _draw_center(draw, col, f_head, pad_x + w_date + w_col * i, w_col,
                     top, h_head, C_HEAD_TX)
    draw.line([pad_x, top + h_head, right, top + h_head], fill=C_HEAD_LINE, width=2 * s)

    # ---------- 数据行 ----------
    f_date, f_num = _font(28 * s), _font(24 * s)
    f_tag = _font(20 * s, True)
    y = top + h_head
    for idx, (date_txt, record, is_today) in enumerate(rows):
        if is_today:
            draw.rectangle([pad_x, y, right, y + h_row], fill=C_TODAY_BG)
        elif idx % 2 == 1:
            draw.rectangle([pad_x, y, right, y + h_row], fill=C_ROW_ALT)

        box = draw.textbbox((0, 0), date_txt, font=f_date)
        draw.text((pad_x + 22 * s, y + (h_row - (box[3] - box[1])) / 2 - box[1]),
                  date_txt, font=f_date, fill=C_TITLE)

        if is_today and today_label:
            tw = _text_width(draw, today_label, f_tag)
            tag_x = pad_x + 22 * s + _text_width(draw, date_txt, f_date) + 12 * s
            tag_y = y + (h_row - 30 * s) / 2
            draw.rounded_rectangle([tag_x, tag_y, tag_x + tw + 16 * s, tag_y + 30 * s],
                                   radius=15 * s, fill=C_TODAY_TAG)
            box2 = draw.textbbox((0, 0), today_label, font=f_tag)
            draw.text((tag_x + 8 * s, tag_y + (30 * s - (box2[3] - box2[1])) / 2 - box2[1]),
                      today_label, font=f_tag, fill=(255, 255, 255))

        for i, col in enumerate(columns):
            text, color = _fmt(record.get(col))
            _draw_center(draw, text, f_num, pad_x + w_date + w_col * i, w_col,
                         y, h_row, color)

        y += h_row
        draw.line([pad_x, y, right, y], fill=C_LINE, width=1 * s)

    # ---------- 边框与竖线 ----------
    draw.rectangle([pad_x, top, right, y], outline=C_BORDER, width=2 * s)
    divider = pad_x + w_date
    draw.line([divider, top, divider, y], fill=C_HEAD_LINE, width=2 * s)
    for i in range(1, len(columns)):
        x = pad_x + w_date + w_col * i
        draw.line([x, top + h_head, x, y], fill=C_LINE, width=1 * s)

    # ---------- 底部注释 ----------
    fy = y + 26 * s
    for line in footnotes:
        draw.text((pad_x, fy), line, font=_font(24 * s), fill=C_FOOT)
        fy += 34 * s

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


def footnotes(source: str, demo: bool = False) -> list[str]:
    now = datetime.now(config.CN_TZ).strftime("%Y-%m-%d %H:%M")
    first = (f"口径：{config.METRIC}净额（单位：{config.UNIT}；"
             "正=净流入占优，红 / 负=净流出占优，绿）")
    origin = "演示数据（非真实行情）" if demo else source
    return [first, f"数据源：{origin} · 生成于 {now}"]


def render_snapshot(hist: dict, trade_date: str, source: str, *,
                    days: int = config.DISPLAY_DAYS, exclude_today: bool = False,
                    demo: bool = False, out_dir: str | None = None) -> str:
    """取最近 days 天数据出图，返回 PNG 路径。"""
    from . import history

    rows = history.recent_rows(hist, trade_date, days)
    if exclude_today:
        rows = [r for r in rows if not r[2]]
    if not rows:
        raise RuntimeError("没有可用于出图的历史数据")

    d0, d1 = rows[0][0], rows[-1][0]
    overall = rows[-1][1].get("所有ETF", 0.0)
    try:
        overall = float(overall)
    except (TypeError, ValueError):
        overall = 0.0
    trend = "净流入占优" if overall >= 0 else "净流出占优"
    title = f"ETF 各指数主力净流入 · 近 {len(rows)} 个交易日（{trend}）"
    subtitle = f"{d0} - {d1} · {config.METRIC}（{config.UNIT}）"

    folder = out_dir or str(config.SNAPSHOT_DIR)
    name = f"etf_flow_{'demo' if demo else trade_date}_{d1.replace('/', '')}.png"
    path = os.path.join(folder, name)

    return render_table(
        rows, config.COLUMNS, title, subtitle,
        footnotes(source, demo=demo), path,
        today_label=None if exclude_today else "今日",
    )
