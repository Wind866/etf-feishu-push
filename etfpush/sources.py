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


# 镜像健康分：失败 +1、成功 -1（下限 0）。
# runner 上实测有些镜像会间歇性返回空响应，把它们的优先级压下去，
# 后面的批次就自动避开，而不是每批都先撞一遍坏主机。
_HOST_FAILS: dict[str, int] = {}


def _mark_host(host: str, ok: bool) -> None:
    if ok:
        _HOST_FAILS[host] = max(0, _HOST_FAILS.get(host, 0) - 1)
    else:
        _HOST_FAILS[host] = min(9, _HOST_FAILS.get(host, 0) + 1)


def _host_cycle(start: int) -> list[str]:
    """按「健康分升序 + 轮转偏移」排序：好主机优先，压力仍分散在各主机。"""
    n = len(EM_HOSTS)
    order = sorted(
        range(n),
        key=lambda i: (_HOST_FAILS.get(EM_HOSTS[i], 0), (i - start) % n),
    )
    return [EM_HOSTS[i] for i in order]


def _fetch_any_host(path: str, params: dict, start: int, timeout: int = 20,
                    expect_json: bool = True, tries: int = HOSTS_PER_REQUEST) -> str:
    """在镜像之间轮换着请求同一个接口，第一个成功的胜出。"""
    last_err: Exception | None = None
    for host in _host_cycle(start)[:tries]:
        try:
            text = http_get(f"https://{host}{path}", params=params, timeout=timeout,
                            retries=1, expect_json=expect_json)
            _mark_host(host, True)
            return text
        except Exception as exc:  # noqa: BLE001
            _mark_host(host, False)
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


def _looks_like_etf(name: str) -> bool:
    """把「混在 ETF 代码段里的场外基金」挡在代码表之外。

    天天基金 fundcode_search.js 是**全量基金表**，501/502/505/506/508 等代码段
    还包含 LOF、定开混合、三年持有、场外 QDII（如「东方红睿玺三年持有混合A」
    「南方原油A」）。原实现只按代码段筛，2287 条里有 423 条是这类非 ETF：
      * 有行情的 LOF 会被算进「所有ETF」，合计被污染；
      * 没行情的会稀释覆盖率分母，让 0.6 的通道闸门变得毫无意义，
        补抓阶段还要为它们白跑两轮。
    场内 ETF 的简称/全称一律带 ETF 字样，用名称再卡一道最稳。
    """
    upper = str(name).upper()
    if "ETF" not in upper:
        return False
    if any(k in upper for k in config.NON_ETF_KEYWORDS):
        return False
    if any(k in str(name) for k in config.CASH_KEYWORDS):
        return False
    return True


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
    dropped = 0
    for item in rows:
        code, name = str(item[0]), str(item[2]).strip()
        if not (_is_etf_code(code) and name):
            continue
        if not _looks_like_etf(name):
            dropped += 1          # LOF / 定开混合 / 场外 QDII / 联接 / 货币类
            continue
        out.append({"secid": _secid(code), "code": code, "name": name})
    if not out:
        raise RuntimeError("基金代码表中未筛出 ETF")
    print(f"  基金代码表：按名称剔除 {dropped} 只非 ETF 品种，保留 {len(out)} 只")
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
            name = "".join(parts[1:])
            # 旧版回写的清单里混有场外基金，降级读回来时同样要过滤，
            # 否则在线源全挂时口径会和平时不一致。
            if not _looks_like_etf(name):
                continue
            out.append({"secid": _secid(parts[0]), "code": parts[0], "name": name})
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


_UNIVERSE: list[dict] | None = None


def get_universe() -> list[dict]:
    """ETF 代码表（四级降级），结果在进程内缓存，避免闸门校验时重复抓。"""
    global _UNIVERSE
    if _UNIVERSE:
        return _UNIVERSE
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
            _UNIVERSE = codes
            return codes
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            print(f"  ⚠️ {name} 代码表获取失败：{exc}")
    raise RuntimeError("ETF 代码表全部获取失败\n" + "\n".join(errors))


def expected_counts() -> dict[str, int] | None:
    """代码表里各宽基指数各有几只 ETF，供质量闸门做「相对」判定。

    绝对只数不能当标准：全市场上证50 只有 11 只，抓到 5 只可能只是漏了一批，
    抓到 0 只才是规则/代码表坏了。所以按「占代码表的比例」判断更准。
    """
    from .aggregate import classify      # 延迟导入：aggregate 只依赖 config，不会循环

    try:
        universe = get_universe()
    except Exception:  # noqa: BLE001
        return None
    out: dict[str, int] = {}
    for item in universe:
        if not _looks_like_etf(item["name"]):
            continue
        col = classify(item["name"])
        if col:
            out[col] = out.get(col, 0) + 1
    return out or None


def universe_stats() -> dict:
    """一次性给出闸门所需的两个基准：各指数只数 + 有效候选总数。

    有效候选 = 代码表里真正会进入「所有ETF」统计的品种数。用它做分母，
    闸门才能随 ETF 数量增长自动收紧，而不是永远卡在 1200 这个绝对值上。
    """
    from .aggregate import classify      # 延迟导入：aggregate 只依赖 config

    try:
        universe = get_universe()
    except Exception:  # noqa: BLE001
        return {}
    indices: dict[str, int] = {}
    total = 0
    for item in universe:
        if not _looks_like_etf(item["name"]):
            continue
        total += 1
        col = classify(item["name"])
        if col:
            indices[col] = indices.get(col, 0) + 1
    print(f"  闸门基准：有效候选 {total} 只 ETF，各宽基 {indices}")
    return {"indices": indices or None, "total": total or None}


# ---------------------------------------------------------------
# 2) 主力净流入 f62（通道一：ulist 批量）
# ---------------------------------------------------------------
BATCH_SIZE = 180          # 一次 180 个 secid，URL 约 2KB；再长有些网关会直接掐断
BATCH_PAUSE = 0.4         # 批次间隔，别把风控打成雪崩
MIN_SPLIT_SIZE = 24       # 二分重试的下限：再小就不值得拆了
MAX_SPLIT_DEPTH = 2       # 180 → 90 → 45，最多拆两层
FILL_CHUNK = 60           # 补抓时每条 URL 的代码数（更短，成功率更高）
FILL_PAUSE = 1.0
FILL_ROUNDS = 2           # 主循环结束后再补抓几轮
FETCH_BUDGET = 420        # 单个通道的总时间预算（秒），超出就带着现有数据走


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


def _flow_batch(secids: list[str], host_start: int, depth: int = 0) -> dict[str, float]:
    """整批取；失败就二分重试。

    实测 runner 上偶尔会整批返回空响应（网关对大 URL 掐连接），
    但同一批拆成两半后通常都能过 —— 所以失败不要直接丢，先拆。
    """
    try:
        return _flow_chunk(secids, host_start)
    except Exception as exc:  # noqa: BLE001
        if len(secids) <= MIN_SPLIT_SIZE or depth >= MAX_SPLIT_DEPTH:
            raise
        mid = len(secids) // 2
        print(f"  ↳ {len(secids)} 只的批次失败，拆成 {mid}+{len(secids) - mid} 重试：{exc}")
        out: dict[str, float] = {}
        last_err: Exception | None = None
        for part in (secids[:mid], secids[mid:]):
            try:
                out.update(_flow_batch(part, host_start + 1, depth + 1))
            except Exception as exc2:  # noqa: BLE001
                last_err = exc2
            time.sleep(BATCH_PAUSE)
        if not out:
            raise (last_err or exc)
        return out


def _fill_missing(codes: list[str], flow: dict[str, float], deadline: float) -> None:
    """主循环里失败的批次会留下缺口，这里只补缺的那部分，直到补不动为止。

    注意缺口有两种：失败批次漏掉的（补得回来），和已清盘/未上市代码
    （永远没有 f62）。所以补一轮没捞回多少就收手，别白跑第二轮。
    """
    for rnd in range(FILL_ROUNDS):
        missing = [c for c in codes if c not in flow]
        if not missing or time.time() > deadline:
            return
        before = len(missing)
        print(f"  🔁 补抓第 {rnd + 1} 轮：{len(missing)} 只还没取到")
        for i in range(0, len(missing), FILL_CHUNK):
            if time.time() > deadline:
                print("  ⏹ 时间预算用完，停止补抓（用现有数据继续）")
                return
            part = missing[i:i + FILL_CHUNK]
            try:
                flow.update(_flow_batch([_secid(c) for c in part],
                                        host_start=rnd * 5 + i // FILL_CHUNK))
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️ 补抓 {len(part)} 只失败：{exc}")
            time.sleep(FILL_PAUSE)
        recovered = before - len([c for c in codes if c not in flow])
        print(f"     本轮补回 {recovered} 只")
        if recovered < before * 0.15:
            print("     补回比例太低，剩下的应已清盘/未上市，停止补抓")
            return


def fetch_eastmoney() -> list[tuple[str, float]]:
    """主链路：代码表 + ulist 批量（失败二分重试 + 缺号补抓）。"""
    t0 = time.time()
    deadline = t0 + FETCH_BUDGET
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
            flow.update(_flow_batch([_secid(c) for c in chunk], host_start=idx))
            consecutive = 0
        except Exception as exc:  # noqa: BLE001
            failed += 1
            consecutive += 1
            print(f"  ⚠️ 第 {idx + 1}/{batches} 批失败（{len(chunk)} 只）：{exc}")
            # 熔断：连续几个批次（含二分重试）全失败，说明不是抖动而是站点不可用，
            # 别再一个批次一个批次地等超时，把时间留给下一个通道。
            if consecutive >= 3 and not flow:
                raise RuntimeError(
                    f"连续 {consecutive} 个批次全部镜像失败，判定 ulist 通道不可用"
                ) from exc
        time.sleep(BATCH_PAUSE)

    if not flow:
        raise RuntimeError("ulist 未返回任何资金流数据")
    if failed:
        print(f"  ℹ️ {failed}/{batches} 个批次初期失败，进入补抓")

    _fill_missing(codes, flow, deadline)

    items = [(name_by_code[c], v) for c, v in flow.items() if c in name_by_code]
    # 分母只用「有效候选」：已清盘 / 停牌 / 代码表里的场外基金本来就没有 f62，
    # 把它们算进分母会低估真实覆盖率；反过来，用固定 0.6 又会放过整批丢失。
    candidates = [c for c in codes if _looks_like_etf(name_by_code[c])]
    got = sum(1 for c in candidates if c in flow)
    cover = got / max(len(candidates), 1)
    print(f"  东财批量：{len(items)}/{len(codes)} 个代码有资金流"
          f"（有效候选覆盖 {got}/{len(candidates)} = {cover:.0%}，"
          f"耗时 {time.time() - t0:.0f}s）")
    if cover < config.MIN_CHANNEL_COVER:
        raise RuntimeError(
            f"有效候选覆盖度过低（{cover:.0%} < {config.MIN_CHANNEL_COVER:.0%}），"
            "判定本通道数据不可信"
        )
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


# ---------------------------------------------------------------
# 5) 历史回补：逐只取「资金流向-日线」，一次请求拿到一整段历史
#    （ulist 只给当日快照，没有历史；日线接口才有。）
#    注意这个接口在 push2his 域名下，镜像跟 push2 不同。
# ---------------------------------------------------------------
# 日线接口（push2his）的频控比实时接口严得多：实测猛拉到 ~368 只就会被整段
# 掐断（之后连 curl 都 rc=56，要等几分钟才恢复）。多铺几个镜像，没被封的镜像
# 仍能出货，被封的那批靠「断点续传 + 跨次运行」慢慢补齐。
EM_HIS_HOSTS = (
    "push2his.eastmoney.com",
    "1.push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
    "13.push2his.eastmoney.com",
    "21.push2his.eastmoney.com",
    "29.push2his.eastmoney.com",
    "33.push2his.eastmoney.com",
    "40.push2his.eastmoney.com",
    "60.push2his.eastmoney.com",
    "70.push2his.eastmoney.com",
    "80.push2his.eastmoney.com",
    "82.push2his.eastmoney.com",
    "90.push2his.eastmoney.com",
    "99.push2his.eastmoney.com",
)
EM_FFLOW_PATH = "/api/qt/stock/fflow/daykline/get"
# f51 日期 / f52 主力净流入 / f53 小单 / f54 中单 / f55 大单 / f56 超大单 …
FFLOW_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"


def _daykline(secid: str, lmt: int, start: int = 0) -> dict[str, float]:
    """单只 ETF 的资金流日线 → {日期: 主力净流入元}。"""
    last_err: Exception | None = None
    for i in range(3):          # 试 3 个镜像：多铺了镜像后，多试能多救回一些被封的请求
        host = EM_HIS_HOSTS[(start + i) % len(EM_HIS_HOSTS)]
        try:
            text = http_get(f"https://{host}{EM_FFLOW_PATH}", params={
                "lmt": lmt, "klt": 101, "secid": secid,
                "fields1": "f1,f2,f3,f7", "fields2": FFLOW_FIELDS,
            }, timeout=15, retries=1, expect_json=True)
            data = (json.loads(text) or {}).get("data") or {}
            out: dict[str, float] = {}
            for line in data.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    out[parts[0]] = float(parts[1])
                except ValueError:
                    continue      # 停牌日 f52 是 "-"
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"日线取数失败 {secid}：{last_err}")


def _load_hist_cache(path) -> dict[str, dict[str, float]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_hist_cache(path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 断点文件写入失败（忽略）：{exc}")


def fetch_history(days: int = 20, workers: int = 3, pace: float = 0.4,
                  cache_path=None, max_passes: int = 5,
                  refresh: bool = False,
                  stale_date: date | None = None) -> dict[str, list[tuple[str, float]]]:
    """回补用：{交易日: [(ETF 名称, 主力净流入元)]}。

    为什么这么小心：日线接口（push2his）的频控比实时接口严得多。
    实测 8 并发猛拉，跑到第 368 只就被整段掐断（之后连 curl 都 rc=56，
    要等几分钟才恢复）。所以这里：
      * 默认 4 并发 + 每只之间 pace 秒间隔，控制在每秒几次；
      * 断点续传：已取到的马上落盘，被封了也不会白跑；
      * 失败的多轮重试，轮间退避，越往后等得越久。
    """
    import concurrent.futures as cf

    universe = get_universe()
    cache_path = cache_path or (config.DATA_DIR / ".hist_cache.json")
    cache = {} if refresh else _load_hist_cache(cache_path)
    if cache:
        print(f"  断点续传：已有 {len(cache)} 只的历史缓存")

    # 缓存原来是「一次抓取、永久复用」：pending 只看 code 在不在 cache 里，
    # 于是第一次跑完之后所有代码都被认为已完成，日线接口再也不会被调用，
    # 新交易日的数据永远补不进来（workflow 还把这份旧缓存强制提交回仓库，
    # 等于把过期状态持久化了）。这里按「缓存里最新的日期」判断是否过期。
    expired = False
    if cache and stale_date and not refresh:
        newest = max((max(series) for series in cache.values() if series), default="")
        if newest and newest < stale_date.isoformat():
            expired = True
            print(f"  ⚠️ 断点缓存最新日期 {newest} 早于最新行情日 {stale_date}，"
                  "判定过期，本轮全量重取（旧值保留作失败时的回退）")
    pending = [u for u in universe if expired or u["code"] not in cache]
    total = len(universe)

    def one(item: dict):
        time.sleep(pace + (hash(item["code"]) % 100) / 1000.0)   # 打散，别形成整齐的脉冲
        try:
            return item["code"], _daykline(item["secid"], lmt=days,
                                           start=hash(item["code"]) % len(EM_HIS_HOSTS))
        except Exception:  # noqa: BLE001 - 单只失败留到下一轮
            return item["code"], None

    for p in range(max_passes):
        if not pending:
            break
        print(f"  第 {p + 1} 轮：待取 {len(pending)} 只（总 {total}）")
        still: list[dict] = []
        done = 0
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(one, u): u for u in pending}
            for fut in cf.as_completed(futs):
                code, series = fut.result()
                done += 1
                if series is None:
                    still.append(futs[fut])
                else:
                    cache[code] = series          # 空 dict 也缓存：说明该代码本来就没数据
                if done % 100 == 0:
                    _save_hist_cache(cache_path, cache)
                    print(f"    进度 {done}/{len(pending)}，已累计 {len(cache)} 只")
        _save_hist_cache(cache_path, cache)
        pending = still
        if pending:
            wait = 5 * (p + 1)
            print(f"  ⚠️ 本轮仍有 {len(pending)} 只失败，等 {wait}s 再重试（多半是被限流）")
            time.sleep(wait)

    if pending:
        print(f"  ℹ️ 断点缓存累计 {len(cache)}/{total} 只；最终仍有 {len(pending)} 只没取到"
              f"（多为已清盘/新建无数据）：{[u['code'] for u in pending[:6]]}")
    else:
        print(f"  ✅ 断点缓存累计 {len(cache)}/{total} 只，本次全部取齐")

    by_date: dict[str, list[tuple[str, float]]] = {}
    for item in universe:
        series = cache.get(item["code"]) or {}
        for d, val in series.items():
            by_date.setdefault(d, []).append((item["name"], val))
    if not by_date:
        raise RuntimeError("日线接口没有返回任何历史数据（可能正被限流，稍后重试）")
    for d in sorted(by_date)[-3:]:
        print(f"    {d}：{len(by_date[d])} 只")
    return by_date


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
# 6) 最近交易日判定（不维护节假日表）
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


def _plausible_quote_date(d, today: date) -> bool:
    """挡住 f124 毫秒/0、腾讯串里抓错 14 位数字等异常时间戳。

    原版把任何解析出来的日期都当真：一个 1970 或 5 年后的日期会被判成
    「今天休市」直接跳过；反过来若探测失败又会「按工作日继续」，
    拿上一交易日的 f62 快照记成今天。两种都是错行。
    """
    if not isinstance(d, date):
        return False
    return -1 <= (today - d).days <= config.SESSION_MAX_STALE_DAYS


def trade_calendar(lmt: int = 40, secid: str = "1.000001") -> list[str]:
    """指数日 K → 最近 lmt 个真实交易日（YYYY-MM-DD，升序）；失败返回 []。

    项目不维护节假日表，但「自动回补缺漏」需要知道哪些日子本该有记录。
    用上证指数日 K 当免费、精确的交易日历：指数每个交易日必有 K 线，
    周末/节假日天然缺席，不需要任何本地节假日逻辑。调用方在探测失败
    （返回 []）时应保守跳过，不要误触发回补。
    """
    try:
        text = http_get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={
            "secid": secid, "klt": 101, "fqt": 1, "lmt": lmt, "end": "20500101",
            "fields1": "f1,f2,f3", "fields2": "f51,f53", "ut": EM_UT,
        }, timeout=15, retries=2, expect_json=True)
        data = (json.loads(text) or {}).get("data") or {}
        klines = data.get("klines") or []
        return sorted(line.split(",")[0] for line in klines if line)
    except Exception as exc:  # noqa: BLE001 - 探测失败交由调用方保守处理
        print(f"  ⚠️ 交易日历探测失败（忽略）：{exc}")
        return []


def latest_trade_date(today: date | None = None) -> date | None:
    """返回「最新一个已有行情的交易日」；两个探测源都失败/异常时返回 None。"""
    today = today or datetime.now(config.CN_TZ).date()
    for name, probe in (("腾讯", _probe_tencent), ("东财", _probe_eastmoney)):
        try:
            d = probe()
            if _plausible_quote_date(d, today):
                return d
            print(f"  ⚠️ 交易日探测（{name}）返回空或日期异常：{d}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 交易日探测（{name}）失败：{exc}")
    return None


def trading_session(today: date) -> tuple[bool, date, str]:
    """(是否继续, 数据日期, 原因)。

    核心修正：**数据日期必须取行情时间戳里的日期，而不是运行时刻的本地日期**。
    GitHub cron 实测会延迟 3~10 小时，一旦跨过午夜，原版 today 变成次日，
    交易日探测又会因「最新行情还停在昨天」判成休市直接跳过 —— 当天数据永久丢失；
    探测双失败时更会把昨天的快照记成今天。用行情日期记账 + 按日期判重，
    延迟、节假日、周末、多档 cron 就全部自洽了。
    """
    lt = latest_trade_date(today)
    if lt is None:
        return False, today, (
            "两个行情时间戳都取不到或日期异常；为避免把上一交易日的快照错记到今天，"
            "本次跳过（确认后可用 --force 强制）"
        )
    if lt > today:
        return False, today, f"行情日期 {lt} 晚于运行日 {today}，时间戳异常，跳过"
    stale = (today - lt).days
    if stale > config.SESSION_MAX_STALE_DAYS:
        return False, today, f"最新行情停在 {lt}（距今 {stale} 天），数据疑似过期，跳过"
    if stale:
        return True, lt, (f"运行日 {today}，最新行情日 {lt}（相差 {stale} 天，"
                          "按行情日期记账；该日若已推送会自动跳过）")
    return True, lt, f"最新行情日期 {lt}，正常交易日"


def is_trading_day(today: date) -> tuple[bool, str]:
    """兼容旧调用：只返回 (是否继续, 原因)，数据日期请用 trading_session。"""
    ok, _data_date, reason = trading_session(today)
    return ok, reason


# ---------------------------------------------------------------
# 7) 体检：逐个镜像 / 接口探活，方便排查"某天没推成功"
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
