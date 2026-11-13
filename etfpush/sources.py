# -*- coding: utf-8 -*-
"""数据源层：拿「全市场 ETF 的主力净流入金额（元）」。

为什么是这套组合
----------------
东财的 `clist/get`（akshare 用的那个接口）在部分网络 / 出口 IP 下会被 WAF
直接掐断 TLS 连接（表现为 `curl: (56) server closed abruptly`），而且在
GitHub Actions 的公共 runner 上尤其不稳定。实测 `ulist.np/get` 这条路
（指定一批 secid 批量取字段）基本不受影响，所以主链路改成：

    1) ETF 代码全表      ← 新浪 etf_hq_fund 节点（东财失败时退回 clist）
    2) 每只 ETF 的 f62   ← 东财 ulist.np 批量取（180 只/请求，约 6 次）
    3) 兜底              ← 东财 clist/get 分页 → akshare fund_etf_spot_em

f62 = 主力净流入净额（元），正=净流入。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import date, datetime

import requests

from . import config

# ---------------------------------------------------------------
# HTTP：requests 优先，失败时用 curl 再试一次
#   有些站点的 WAF 按 TLS 指纹拦 Python-requests，curl 却能过；
#   反过来也有人遇到 curl 被拦。两个都试，覆盖面最大。
#   加 `-4` 强制 IPv4：runner 解析到异常 IPv6 时连接会被对端重置。
# ---------------------------------------------------------------
_CURL_OK: bool | None = None


def _has_curl() -> bool:
    global _CURL_OK
    if _CURL_OK is None:
        try:
            _CURL_OK = subprocess.run(
                ["curl", "--version"], capture_output=True, timeout=5
            ).returncode == 0
        except Exception:
            _CURL_OK = False
    return _CURL_OK


def _via_requests(url: str, params: dict | None, timeout: int) -> str:
    resp = requests.get(url, params=params, headers=config.HTTP_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _via_curl(url: str, params: dict | None, timeout: int) -> str:
    if not _has_curl():
        raise RuntimeError("curl 不可用")
    full = requests.Request("GET", url, params=params).prepare().url
    cmd = ["curl", "-4", "-sS", "-f", "--compressed", "--max-time", str(timeout)]
    for k, v in config.HTTP_HEADERS.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(full)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout + 5,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl rc={proc.returncode}: {(proc.stderr or '').strip()[:120]}")
    if not proc.stdout.strip():
        raise RuntimeError("curl 返回空响应")
    return proc.stdout


def http_get(url: str, params: dict | None = None, timeout: int = 15,
             retries: int = 3, expect_json: bool = False) -> str:
    """GET，返回文本；失败自动在 requests / curl 之间轮换并重试。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        for fn in (_via_requests, _via_curl):
            try:
                text = fn(url, params, timeout)
                if expect_json and text.lstrip()[:1] not in ("{", "["):
                    raise RuntimeError(f"返回非 JSON：{text[:80]}")
                return text
            except Exception as exc:  # noqa: BLE001 - 换下一个通道继续试
                last_err = exc
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))   # 线性退避，避免把限流打成雪崩
    raise RuntimeError(f"请求失败 {url}：{type(last_err).__name__}: {last_err}")


# ---------------------------------------------------------------
# 1) ETF 代码全表
# ---------------------------------------------------------------
# 天天基金「全部基金代码」js：约 2.8 万个基金，含名称，一次请求拿全。
# 场内 ETF 用代码段过滤（沪 50/51/52/53/55/56/58 开头，深 159 开头）。
# 这份表更新及时，A500、科创50 等新品种都在；新浪的 ETF 板块列表会漏新ETF，
# 只作备用。表里也含大量已清盘/未上市代码，取不到资金流会自动跳过。
EM_FUNDLIST_JS = "https://fund.eastmoney.com/js/fundcode_search.js"
SH_PREFIXES = ("50", "51", "52", "53", "55", "56", "58")

SINA_NODE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"   # 东财公开 ut，缺省时大页会被截断


def _is_etf_code(code: str) -> bool:
    return code.startswith("159") or code[:2] in SH_PREFIXES


def _secid(code: str) -> str:
    """代码 → 东财 secid：1.=沪市 / 0.=深市。"""
    return ("0." if code.startswith("159") else "1.") + code


def universe_from_fundlist() -> list[dict]:
    """天天基金全量基金表 → 场内 ETF 代码表。"""
    text = http_get(EM_FUNDLIST_JS, timeout=30, retries=2)
    match = re.search(r"\[\[.*\]\]", text, re.S)
    if not match:
        raise RuntimeError("基金代码表格式变化，未匹配到数组")
    rows = json.loads(match.group(0))
    out = []
    for item in rows:
        code, name = str(item[0]), str(item[2]).strip()
        if _is_etf_code(code) and name:
            out.append({"secid": _secid(code), "code": code, "name": name})
    if not out:
        raise RuntimeError("基金代码表中未筛出 ETF")
    return out


def _secid_from_symbol(symbol: str) -> str:
    """新浪 symbol（sh510300 / sz159915）→ 东财 secid（1.510300 / 0.159915）。"""
    market = {"sh": "1", "sz": "0", "bj": "0"}.get(symbol[:2].lower())
    if not market:
        return ""
    return f"{market}.{symbol[2:]}"


def universe_from_sina(max_pages: int = 40) -> list[dict]:
    """新浪 ETF 板块列表，返回 [{secid, code, name}]。实测 ~930 只。"""
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        text = http_get(
            SINA_NODE_URL,
            params={"page": page, "num": 100, "sort": "symbol", "asc": 1,
                    "node": "etf_hq_fund", "_s_r_a": "page"},
            timeout=15, retries=2,
        ).strip()
        if text in ("null", "[]", ""):
            break
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            break
        if not items:
            break
        for it in items:
            symbol = str(it.get("symbol") or "")
            name = str(it.get("name") or "").strip()
            secid = _secid_from_symbol(symbol)
            if not (secid and name) or secid in seen:
                continue
            seen.add(secid)
            out.append({"secid": secid, "code": symbol[2:], "name": name})
        if len(items) < 100:
            break
    if not out:
        raise RuntimeError("新浪 ETF 列表为空")
    return out


def universe_from_eastmoney(page_size: int = 200) -> list[dict]:
    """东财 clist 取 ETF 列表（新浪不可用时的备用）。"""
    out: list[dict] = []
    page, total = 1, None
    while True:
        text = http_get(
            EM_CLIST,
            params={"pn": page, "pz": page_size, "po": 1, "np": 1, "ut": EM_UT,
                    "fltt": 2, "invt": 2, "fid": "f12",
                    # 沪市 ETF / 深市 ETF / 跨市场 ETF 等板块
                    "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
                    "fields": "f12,f13,f14"},
            timeout=20, retries=2, expect_json=True,
        )
        data = (json.loads(text) or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        if total is None:
            total = int(data.get("total") or 0)
        for d in diff:
            code = str(d.get("f12") or "")
            name = str(d.get("f14") or "").strip()
            market = str(d.get("f13") or ("1" if code.startswith(("5", "6")) else "0"))
            if code and name:
                out.append({"secid": f"{market}.{code}", "code": code, "name": name})
        if total and len(out) >= total:
            break
        page += 1
        time.sleep(0.4)
    if not out:
        raise RuntimeError("东财 ETF 列表为空")
    return out


def get_universe() -> list[dict]:
    errors = []
    for name, fn in (("天天基金", universe_from_fundlist),
                     ("新浪", universe_from_sina),
                     ("东财", universe_from_eastmoney)):
        try:
            codes = fn()
            print(f"  ETF 代码表：{name} 共 {len(codes)} 个代码")
            return codes
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            print(f"  ⚠️ {name} 代码表获取失败：{exc}")
    raise RuntimeError("ETF 代码表全部获取失败\n" + "\n".join(errors))


# ---------------------------------------------------------------
# 2) 主力净流入（东财 f62），批量取
# ---------------------------------------------------------------
EM_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
BATCH_SIZE = 180          # 一次 180 个 secid，URL 约 2KB；再长有些网关会直接掐断
BATCH_PAUSE = 0.5         # 批次间隔，避免把风控打成雪崩


def _flow_chunk(secids: list[str]) -> dict[str, float]:
    text = http_get(
        EM_ULIST,
        params={"fltt": 2, "invt": 2, "secids": ",".join(secids),
                "fields": "f12,f14,f62"},
        timeout=25, retries=3, expect_json=True,
    )
    data = (json.loads(text) or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out: dict[str, float] = {}
    for d in diff:
        code = str(d.get("f12") or "")
        try:
            out[code] = float(d.get("f62"))
        except (TypeError, ValueError):
            continue            # 停牌 / 未上市时 f62 是 "-"
    return out


def fetch_eastmoney() -> list[tuple[str, float]]:
    """主链路：代码表 + 东财 ulist 批量 f62。个别批次失败不影响整体。"""
    universe = get_universe()
    name_by_code = {u["code"]: u["name"] for u in universe}
    codes = [u["code"] for u in universe]

    flow: dict[str, float] = {}
    failed = 0
    batches = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
    for idx in range(batches):
        chunk = codes[idx * BATCH_SIZE:(idx + 1) * BATCH_SIZE]
        try:
            flow.update(_flow_chunk([_secid(c) for c in chunk]))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ⚠️ 第 {idx + 1}/{batches} 批失败（{len(chunk)} 只）：{exc}")
        time.sleep(BATCH_PAUSE)

    if not flow:
        raise RuntimeError("东财 ulist 未返回任何资金流数据")
    if failed:
        print(f"  ℹ️ {failed}/{batches} 个批次失败，已用其余批次数据继续")

    items = [(name_by_code[c], v) for c, v in flow.items() if c in name_by_code]
    print(f"  东财批量：{len(items)} 只有资金流数据（代码表共 {len(codes)} 个代码）")
    return items


def fetch_eastmoney_clist() -> list[tuple[str, float]]:
    """备用：clist 分页（akshare 同款接口，部分网络下会被 WAF 掐断）。"""
    out: list[tuple[str, float]] = []
    page, total, page_size = 1, None, 100
    while True:
        text = http_get(
            EM_CLIST,
            params={"pn": page, "pz": page_size, "po": 1, "np": 1, "ut": EM_UT,
                    "fltt": 2, "invt": 2, "fid": "f12",
                    "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
                    "fields": "f12,f14,f62"},
            timeout=20, retries=2, expect_json=True,
        )
        data = (json.loads(text) or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        if total is None:
            total = int(data.get("total") or 0)
        for d in diff:
            name, raw = d.get("f14"), d.get("f62")
            try:
                out.append((str(name), float(raw)))
            except (TypeError, ValueError):
                continue
        if total and len(out) >= total * 0.95:
            break
        page += 1
        time.sleep(0.5)
    if not out:
        raise RuntimeError("东财 clist 未返回数据")
    return out


def fetch_akshare() -> list[tuple[str, float]]:
    """兜底：akshare 封装（需 pip install akshare，未安装时直接跳到失败）。"""
    import akshare as ak  # noqa: PLC0415 - 延迟导入，未安装不影响主链路

    df = ak.fund_etf_spot_em()
    out: list[tuple[str, float]] = []
    for name, val in zip(df["名称"], df["主力净流入-净额"]):
        try:
            out.append((str(name), float(val)))
        except (TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError("akshare 返回空数据")
    return out


SOURCES = (
    ("东财·批量", fetch_eastmoney),
    ("东财·分页", fetch_eastmoney_clist),
    ("akshare·东财", fetch_akshare),
)


def fetch_flow() -> tuple[list[tuple[str, float]], str]:
    """按优先级依次尝试，返回 ([(ETF名称, 主力净流入元)], 命中数据源名)。"""
    errors = []
    for name, fn in SOURCES:
        try:
            items = fn()
            if items:
                print(f"✅ 数据源命中：{name}（{len(items)} 只 ETF）")
                return items, name
        except Exception as exc:  # noqa: BLE001
            msg = f"{name} 失败：{type(exc).__name__}: {exc}"
            print(f"  ❌ {msg}")
            errors.append(msg)
    raise RuntimeError("所有数据源均失败\n" + "\n".join(errors))


# ---------------------------------------------------------------
# 3) 最近交易日判定（不维护节假日表）
#   拿上证指数快照里的时间戳：如果最新行情的日期不是今天，说明今天休市
#   （节假日 / 周末），直接跳过推送，避免把昨天的数据当今天发出去。
# ---------------------------------------------------------------
TENCENT_QUOTE = "https://qt.gtimg.cn/q=sh000001"


def latest_trade_date(timeout: int = 10) -> date | None:
    """返回「最新一个已有行情的交易日」；取不到时返回 None。"""
    try:
        text = http_get(TENCENT_QUOTE, timeout=timeout, retries=2)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 交易日探测失败（按工作日兜底）：{exc}")
        return None
    # v_sh000001="1~上证指数~000001~...~~20261113161427~..."
    m = re.search(r"~(\d{14})~", text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").date()
    except ValueError:
        return None


def is_trading_day(today: date) -> tuple[bool, str]:
    """(是否交易日, 原因说明)。判定不依赖节假日表，只看行情时间戳。"""
    if today.weekday() >= 5:
        return False, "今天是周末，A 股休市"
    lt = latest_trade_date()
    if lt is None:
        return True, "行情时间戳取不到，按工作日继续（数据本身有去重保护）"
    if lt == today:
        return True, f"最新行情日期 {lt}，正常交易日"
    return False, f"最新行情还停在 {lt}，今天休市（节假日），跳过推送"
