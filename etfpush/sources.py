# -*- coding: utf-8 -*-
"""数据源层：拿「全市场 ETF 的主力净流入金额（元）」。

为什么要这么多层
----------------
1. 主力净流入这个指标，只有东财一家公开可用：
   腾讯 `ff_` 接口不支持 ETF（直接返回 none_match），新浪的按代码接口
   对 ETF 会返回符号相反的错值（实测 510050 东财 +0.50亿 / 新浪 -5.33亿），
   混用等于把表格画错。所以"多数据源"只能是东财的多通道 + 多镜像。
2. 东财的接入点很多（push2 / push2delay / 1.push2 … 共 15 个镜像，
   后端 svr 号各不相同），单个被限流或挡掉时换一个通常就能过。
   每个批次轮换起始镜像，天然分摊压力。
3. 通道有三条：ulist 批量（主）、clist 分页（备）、akshare（备）。
4. 代码表四级降级：天天基金全量基金表 → 东财 clist → 新浪板块 →
   仓库内置 data/etf_codes.txt（离线保险）。
5. 源的选择不只"有没有数据"，还要过数据质量闸门（见 aggregate.sanity_check），
   不合格自动换下一个源。

口径：f62 = 主力净流入净额（元），正 = 净流入。
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
             retries: int = 2, expect_json: bool = False) -> str:
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
            time.sleep(1.5 * (attempt + 1))   # 线性退避，别把限流打成雪崩
    raise RuntimeError(f"请求失败 {url}：{type(last_err).__name__}: {last_err}")


# ---------------------------------------------------------------
# 东财镜像主机
# ---------------------------------------------------------------
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"   # 东财公开 ut，缺省时大页会被截断

EM_HOSTS = (
    "push2.eastmoney.com",
    "push2delay.eastmoney.com",      # 延时行情源，收盘 20:00 抓无影响
    "82.push2.eastmoney.com",
    "99.push2.eastmoney.com",
    "1.push2.eastmoney.com",
    "7.push2.eastmoney.com",
    "13.push2.eastmoney.com",
    "21.push2.eastmoney.com",
    "29.push2.eastmoney.com",
    "33.push2.eastmoney.com",
    "40.push2.eastmoney.com",
    "60.push2.eastmoney.com",
    "70.push2.eastmoney.com",
    "80.push2.eastmoney.com",
    "90.push2.eastmoney.com",
)
EM_ULIST_PATH = "/api/qt/ulist.np/get"
EM_CLIST_PATH = "/api/qt/clist/get"
HOSTS_PER_REQUEST = 3       # 单个请求最多换几个镜像，避免东财整体故障时无限重试

LOCAL_CODES_PATH = config.DATA_DIR / "etf_codes.txt"


def _host_cycle(start: int) -> list[str]:
    """从 start 开始轮转镜像：不同批次落在不同主机，天然分散压力。"""
    n = len(EM_HOSTS)
    return [EM_HOSTS[(start + i) % n] for i in range(n)]


def _fetch_any_host(path: str, params: dict, start: int, timeout: int = 20,
                    expect_json: bool = True) -> str:
    """在镜像之间轮换着请求同一个接口，第一个成功的胜出。"""
    last_err: Exception | None = None
    for host in _host_cycle(start)[:HOSTS_PER_REQUEST]:
        try:
            return http_get(f"https://{host}{path}", params=params, timeout=timeout,
                            retries=1, expect_json=expect_json)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"{path} 全镜像失败：{last_err}")


# ---------------------------------------------------------------
# 1) ETF 代码全表（四级降级）
# ---------------------------------------------------------------
# 天天基金「全部基金代码」js：约 2.8 万个基金，含名称，一次请求拿全。
# 场内 ETF 用代码段过滤（沪 50/51/52/53/55/56/58 开头，深 159 开头）。
# 这份表更新及时，A500、科创50 等新品种都在；新浪的 ETF 板块列表会漏新ETF。
EL_ETF = "https://fund.eastmoney.com/js/fundcode_search.js"
SH_PREFIXES = ("50", "51", "52", "53", "55", "56", "58")
SINA_NODE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)


def _is_etf_code(code: str) -> bool:
    return code.startswith("159") or code[:2] in SH_PREFIXES


def _secid(code: str) -> str:
    """代码 → 东财 secid：1.=沪市 / 0.=深市。"""
    return ("0." if code.startswith("159") else "1.") + code


def _secid_from_symbol(symbol: str) -> str:
    """新浪 symbol（sh510300 / sz159915）→ 东财 secid（1.510300 / 0.159915）。"""
    market = {"sh": "1", "sz": "0", "bj": "0"}.get(symbol[:2].lower())
    if not market:
        return ""
    return f"{market}.{symbol[2:]}"


def universe_from_fundlist() -> list[dict]:
    """天天基金全量基金表 → 场内 ETF 代码表（首选）。"""
    text = http_get(EL_ETF, timeout=30, retries=2)
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


def universe_from_eastmoney(page_size: int = 200) -> list[dict]:
    """东财 clist 板块列表（镜像轮换）。"""
    out: list[dict] = []
    page, total = 1, None
    while True:
        text = _fetch_any_host(EM_CLIST_PATH, {
            "pn": page, "pz": page_size, "po": 1, "np": 1, "ut": EM_UT,
            "fltt": 2, "invt": 2, "fid": "f12",
            # 沪市 ETF / 深市 ETF / 跨市场 ETF 等板块
            "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
            "fields": "f12,f13,f14",
        }, start=page)
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


def universe_from_sina(max_pages: int = 40) -> list[dict]:
    """新浪 ETF 板块列表（实测约 930 只，会漏新 ETF，只作备用）。"""
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


def universe_from_local() -> list[dict]:
    """仓库内置清单（data/etf_codes.txt）：所有在线来源都失效时的离线保险。"""
    if not LOCAL_CODES_PATH.exists():
        raise RuntimeError("缺少 data/etf_codes.txt")
    out = []
    with open(LOCAL_CODES_PATH, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2 or not _is_etf_code(parts[0]):
                continue
            out.append({"secid": _secid(parts[0]), "code": parts[0],
                        "name": "".join(parts[1:])})
    if not out:
        raise RuntimeError("data/etf_codes.txt 内容为空")
    return out


def save_universe(universe: list[dict]) -> None:
    """把在线拿到的最新代码表回写本地清单，保证离线兜底不会越来越旧。"""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOCAL_CODES_PATH, "w", encoding="utf-8") as f:
            # 按代码排序写，内容不变时 git 就不会产生无意义的 diff
            for item in sorted(universe, key=lambda u: u["code"]):
                f.write(f"{item['code']} {item['name']}\n")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 本地代码清单更新失败（忽略）：{exc}")


def get_universe() -> list[dict]:
    errors = []
    tiers = (
        ("天天基金", universe_from_fundlist),
        ("东财", universe_from_eastmoney),
        ("新浪", universe_from_sina),
        ("本地清单", universe_from_local),
    )
    for name, fn in tiers:
        try:
            codes = fn()
            print(f"  ETF 代码表：{name} 共 {len(codes)} 个代码")
            if name in ("天天基金", "东财"):
                save_universe(codes)
            return codes
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            print(f"  ⚠️ {name} 代码表获取失败：{exc}")
    raise RuntimeError("ETF 代码表全部获取失败\n" + "\n".join(errors))


# ---------------------------------------------------------------
# 2) 主力净流入 f62（通道一：ulist 批量）
# ---------------------------------------------------------------
BATCH_SIZE = 180          # 一次 180 个 secid，URL 约 2KB；再长有些网关会直接掐断
BATCH_PAUSE = 0.4         # 批次间隔，别把风控打成雪崩


def _flow_chunk(secids: list[str], host_start: int) -> dict[str, float]:
    text = _fetch_any_host(EM_ULIST_PATH, {
        "fltt": 2, "invt": 2, "secids": ",".join(secids), "fields": "f12,f14,f62",
    }, start=host_start, timeout=15)
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
    """主链路：代码表 + ulist 批量。每个批次轮换镜像，个别批次失败不影响整体。"""
    universe = get_universe()
    name_by_code = {u["code"]: u["name"] for u in universe}
    codes = [u["code"] for u in universe]

    flow: dict[str, float] = {}
    failed = 0
    consecutive = 0
    batches = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
    for idx in range(batches):
        chunk = codes[idx * BATCH_SIZE:(idx + 1) * BATCH_SIZE]
        try:
            flow.update(_flow_chunk([_secid(c) for c in chunk], host_start=idx))
            consecutive = 0
        except Exception as exc:  # noqa: BLE001
            failed += 1
            consecutive += 1
            print(f"  ⚠️ 第 {idx + 1}/{batches} 批失败（{len(chunk)} 只）：{exc}")
            # 熔断：连续几个批次全镜像失败，说明不是抖动而是站点不可用，
            # 别再一个批次一个批次地等超时，把时间留给下一个通道。
            if consecutive >= 3 and not flow:
                raise RuntimeError(
                    f"连续 {consecutive} 个批次全部镜像失败，判定 ulist 通道不可用"
                ) from exc
        time.sleep(BATCH_PAUSE)

    if not flow:
        raise RuntimeError("ulist 未返回任何资金流数据")
    if failed:
        print(f"  ℹ️ {failed}/{batches} 个批次失败，已用其余批次数据继续")

    items = [(name_by_code[c], v) for c, v in flow.items() if c in name_by_code]
    print(f"  东财批量：{len(items)} 只有资金流数据（代码表共 {len(codes)} 个代码）")
    return items


# ---------------------------------------------------------------
# 3) 通道二：clist 分页（akshare 同款接口，部分网络会被 WAF 掐断）
# ---------------------------------------------------------------
def fetch_eastmoney_clist() -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    page, total, page_size = 1, None, 100
    page_fail = 0
    while True:
        try:
            text = _fetch_any_host(EM_CLIST_PATH, {
                "pn": page, "pz": page_size, "po": 1, "np": 1, "ut": EM_UT,
                "fltt": 2, "invt": 2, "fid": "f12",
                "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
                "fields": "f12,f14,f62",
            }, start=page)
        except Exception as exc:  # noqa: BLE001
            page_fail += 1
            if page_fail >= 2 or not out:
                raise RuntimeError(f"clist 第 {page} 页全镜像失败：{exc}") from exc
            print(f"  ⚠️ clist 第 {page} 页失败，重试一次：{exc}")
            time.sleep(2)
            continue
        page_fail = 0
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
        raise RuntimeError("clist 未返回数据")
    return out


# ---------------------------------------------------------------
# 4) 通道三：akshare 封装（未安装则自动跳过）
# ---------------------------------------------------------------
def fetch_akshare() -> list[tuple[str, float]]:
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


def fetch_flow(validate=None) -> tuple[list[tuple[str, float]], str]:
    """按优先级尝试各数据源，返回 ([(ETF名称, 主力净流入元)], 命中源名)。

    validate(items) 用来做数据质量校验（数量、各指数命中数），抛异常即视为
    这个源不合格，自动换下一个 —— 避免"接口能通但数据残缺"被当成成功。
    """
    errors = []
    for name, fn in SOURCES:
        try:
            items = fn()
            if not items:
                raise RuntimeError("返回空数据")
            if validate:
                try:
                    validate(items)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"数据质量不达标：{exc}") from exc
            print(f"✅ 数据源命中：{name}（{len(items)} 只 ETF）")
            return items, name
        except Exception as exc:  # noqa: BLE001
            msg = f"{name} 失败：{type(exc).__name__}: {exc}"
            print(f"  ❌ {msg}")
            errors.append(msg)
    raise RuntimeError("所有数据源均失败\n" + "\n".join(errors))


# ---------------------------------------------------------------
# 5) 最近交易日判定（不维护节假日表）
#   读上证指数快照里的更新时间：日期不是今天，说明今天休市（周末/节假日），
#   直接跳过推送，避免把昨天的数据当今天发出去。
#   两个独立探测源：腾讯行情、东财 f124 时间戳。
# ---------------------------------------------------------------
TENCENT_QUOTE = "https://qt.gtimg.cn/q=sh000001"


def _probe_tencent() -> date | None:
    text = http_get(TENCENT_QUOTE, timeout=10, retries=2)
    # v_sh000001="1~上证指数~000001~...~~20261113161427~..."
    m = re.search(r"~(\d{14})~", text)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").date()


def _probe_eastmoney() -> date | None:
    text = _fetch_any_host(EM_ULIST_PATH, {
        "fltt": 2, "secids": "1.000001", "fields": "f14,f124",
    }, start=0, timeout=10)
    data = (json.loads(text) or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    if not diff or not diff[0].get("f124"):
        return None
    return datetime.fromtimestamp(int(diff[0]["f124"]), config.CN_TZ).date()


def latest_trade_date() -> date | None:
    """返回「最新一个已有行情的交易日」；两个探测源都失败时返回 None。"""
    for name, probe in (("腾讯", _probe_tencent), ("东财", _probe_eastmoney)):
        try:
            d = probe()
            if d:
                return d
            print(f"  ⚠️ 交易日探测（{name}）返回空")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 交易日探测（{name}）失败：{exc}")
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


# ---------------------------------------------------------------
# 6) 体检：逐个镜像 / 接口探活，方便排查"某天没推成功"
# ---------------------------------------------------------------
def probe() -> list[tuple[str, bool, str]]:
    """返回 [(检查项, 是否可用, 细节)]，供 `python push.py --probe` 打印。"""
    results: list[tuple[str, bool, str]] = []

    lt = latest_trade_date()
    results.append(("交易日探测(腾讯/东财)", lt is not None, str(lt or "两个源都没响应")))

    sample = "1.510300,0.159915"
    for host in EM_HOSTS:
        t0 = time.time()
        try:
            text = http_get(f"https://{host}{EM_ULIST_PATH}",
                            params={"fltt": 2, "secids": sample, "fields": "f14,f62"},
                            timeout=8, retries=1, expect_json=True)
            ok = '"f62"' in text
            results.append((f"镜像 {host}", ok,
                            f"{int((time.time() - t0) * 1000)}ms" if ok else "返回无 f62"))
        except Exception as exc:  # noqa: BLE001
            results.append((f"镜像 {host}", False, str(exc)[:70]))

    for name, fn in (("代码表 天天基金", universe_from_fundlist),
                     ("代码表 东财clist", universe_from_eastmoney),
                     ("代码表 新浪", universe_from_sina),
                     ("代码表 本地清单", universe_from_local)):
        t0 = time.time()
        try:
            codes = fn()
            results.append((name, bool(codes),
                            f"{len(codes)} 条 / {int((time.time() - t0) * 1000)}ms"))
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, str(exc)[:70]))

    try:
        import akshare  # noqa: F401,PLC0415 - 只检查是否安装
        results.append(("akshare 兜底通道", True, "已安装，多一条独立通道"))
    except ImportError:
        results.append(("akshare 兜底通道", False, "未安装（pip install akshare 可再加一条）"))

    return results
