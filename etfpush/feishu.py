# -*- coding: utf-8 -*-
"""飞书推送：自定义机器人 Webhook（签名校验）+ 可选图片消息。

两条通道：
  1) 图片消息（推荐）—— 需要飞书自建应用凭证上传图片拿 image_key，
     再用群机器人 webhook 发出。效果与参考图一致，点开可放大。
  2) 卡片消息 —— 只用 webhook 就能发，卡片 2.0 原生表格组件；失败再降级
     为逐行文本卡片（老版本客户端兜底）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import requests

from . import config

TIMEOUT = 15


# ---------------------------------------------------------------
# 签名
# ---------------------------------------------------------------
def sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人签名：HmacSHA256(key="", msg="timestamp\\nsecret")。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _with_sign(payload: dict) -> dict:
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = sign(secret, ts)
    return payload


def webhook() -> str:
    return os.environ.get("FEISHU_WEBHOOK", "").strip()


def send(payload: dict) -> bool:
    """发一条消息到群机器人。未配置 webhook 时视为「本地预览」，返回 False。"""
    url = webhook()
    if not url:
        print("⚠️ 未配置 FEISHU_WEBHOOK，本次只出图不推送")
        return False
    resp = requests.post(url, json=_with_sign(payload), timeout=TIMEOUT)
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"飞书返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}") from exc
    code = data.get("code", data.get("StatusCode"))
    if code not in (0, "0", None):
        # 19021 只会在「机器人开了加签但仓库没配密钥」时出现，HTTP 仍是 200，
        # 单看返回体看不出该去配 FEISHU_SECRET，这里直接把结论写进异常。
        hint = "（19021 = 机器人开启了加签，请配置 FEISHU_SECRET）" \
            if code in (19021, "19021") else ""
        raise RuntimeError(f"飞书推送失败：{data}{hint}")
    print(f"✅ 飞书推送成功（{payload.get('msg_type')}）")
    return True


# ---------------------------------------------------------------
# 图片消息
# ---------------------------------------------------------------
def _tenant_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败：{data}")
    return data["tenant_access_token"]


def _upload_image(png_path: str, token: str) -> str:
    with open(png_path, "rb") as f:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("etf_flow.png", f.read(), "image/png")},
            timeout=30,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"上传图片失败：{data}")
    return data["data"]["image_key"]


def send_image(png_path: str) -> bool:
    """推纯图片消息；缺自建应用凭证时返回 False（调用方降级为卡片）。"""
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not (app_id and app_secret):
        print("ℹ️ 未配置 FEISHU_APP_ID / FEISHU_APP_SECRET（上传图片必需），改用卡片消息")
        return False
    if not webhook():
        print("⚠️ 未配置 FEISHU_WEBHOOK，本次只出图不推送")
        return False
    image_key = _upload_image(png_path, _tenant_token(app_id, app_secret))
    print(f"🖼 图片已上传：{image_key}")
    return send({"msg_type": "image",
                 "content": json.dumps({"image_key": image_key})})


# ---------------------------------------------------------------
# 卡片消息（无自建应用时的默认形态）
# ---------------------------------------------------------------
NOTE = f"正=净流入(红) / 负=净流出(绿)，单位：{config.UNIT} · 数据源：东财主力净流入"


def _cell(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "-"
    if n != n:
        return "-"
    if n > 0:
        return f"<font color='red'>+{n:,.2f}亿</font>"
    if n < 0:
        return f"<font color='green'>{n:,.2f}亿</font>"
    return "0.00亿"


def build_card_table(rows, trade_date: str, title_prefix: str = "📊 ETF 主力资金净流入") -> dict:
    """卡片 2.0：原生表格组件，单元格支持 markdown 着色。"""
    total = rows[-1][1].get("所有ETF", 0) if rows else 0
    try:
        header = "red" if float(total) >= 0 else "green"
    except (TypeError, ValueError):
        header = "blue"

    columns = [{"name": "date", "display_name": "日期", "data_type": "text",
                "horizontal_align": "center", "width": "auto"}]
    for col in config.COLUMNS:
        columns.append({"name": col, "display_name": col, "data_type": "markdown",
                        "horizontal_align": "center", "width": "auto"})

    body_rows = []
    for date_txt, record, is_today in rows:
        item = {"date": date_txt + ("（今日）" if is_today else "")}
        for col in config.COLUMNS:
            item[col] = _cell(record.get(col))
        body_rows.append(item)

    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"{title_prefix} {trade_date[5:]}（{config.UNIT}）"},
                "template": header,
            },
            "body": {"elements": [
                {"tag": "table", "page_size": max(len(body_rows), 1),
                 "columns": columns, "rows": body_rows},
                {"tag": "markdown", "content": NOTE},
            ]},
        },
    }


def build_card_text(rows, trade_date: str, title_prefix: str = "📊 ETF 主力资金净流入") -> dict:
    """老版卡片兜底：逐行文本，不依赖表格组件。"""
    total = rows[-1][1].get("所有ETF", 0) if rows else 0
    try:
        header = "red" if float(total) >= 0 else "green"
    except (TypeError, ValueError):
        header = "blue"

    lines = []
    for date_txt, record, is_today in rows:
        parts = [f"**{date_txt}{'（今日）' if is_today else ''}**"]
        for col in config.COLUMNS:
            parts.append(f"{col} {_cell(record.get(col))}")
        lines.append(" · ".join(parts))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"{title_prefix} {trade_date[5:]}（{config.UNIT}）"},
                "template": header,
            },
            "elements": [
                {"tag": "markdown", "content": "\n---\n".join(lines)},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": NOTE}]},
            ],
        },
    }


def send_card(rows, trade_date: str) -> bool:
    """先发表格卡片，失败退回逐行卡片。"""
    try:
        return send(build_card_table(rows, trade_date))
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 表格卡片发送失败，降级为逐行卡片：{exc}")
        return send(build_card_text(rows, trade_date))


def notify_failure(reason: str) -> None:
    """任务失败时往群里推一条文字告警，别让失败只留在 Actions 日志里。"""
    if os.environ.get("ALERT_ON_FAIL", "1") != "1" or not webhook():
        return
    try:
        requests.post(webhook(), json=_with_sign({
            "msg_type": "text",
            "content": {"text": f"⚠️ ETF 资金推送失败\n{reason[:800]}"},
        }), timeout=10)
        print("📨 已发送失败告警")
    except Exception as exc:  # noqa: BLE001
        print(f"告警发送失败（忽略）：{exc}")
