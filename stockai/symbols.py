"""标的解析：把 "苹果" / "aapl" / "BTC" / "sol-usd" / "crypto:PEPE" 统一解析成标准代码。

规则：
1. 显式前缀：crypto:XXX / stock:XXX
2. 带 -USD / -USDT / /USDT 后缀 -> 加密
3. 常见加密币种列表或中文别名 -> 加密
4. 中文股票别名表 -> 美股
5. 先当美股代码查 yfinance；查不到再到交易所查是否有 XXX/USDT；仍然没有就用 yfinance 搜索
"""
from __future__ import annotations

import re
from typing import Any

from .cache import cached

CN_STOCK_ALIASES = {
    "苹果": "AAPL", "英伟达": "NVDA", "特斯拉": "TSLA", "微软": "MSFT", "谷歌": "GOOGL",
    "亚马逊": "AMZN", "脸书": "META", "台积电": "TSM", "阿里巴巴": "BABA", "阿里": "BABA",
    "拼多多": "PDD", "京东": "JD", "百度": "BIDU", "网易": "NTES", "超微": "AMD", "博通": "AVGO",
    "奈飞": "NFLX", "伯克希尔": "BRK-B", "英特尔": "INTC", "高通": "QCOM", "甲骨文": "ORCL",
    "美光": "MU", "帕兰提尔": "PLTR", "可口可乐": "KO", "麦当劳": "MCD", "沃尔玛": "WMT",
    "摩根大通": "JPM", "蔚来": "NIO", "小鹏": "XPEV", "理想": "LI", "富途": "FUTU", "老虎": "TIGR",
    "微策略": "MSTR", "比特大陆": "BITF", "coinbase": "COIN", "标普500": "SPY", "标普": "SPY",
    "纳指": "QQQ", "纳斯达克": "QQQ", "道指": "DIA", "罗素2000": "IWM", "恐慌指数": "^VIX",
    "黄金": "GLD", "白银": "SLV", "原油": "USO", "美债": "TLT", "半导体": "SMH",
}

CN_CRYPTO_ALIASES = {
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "狗狗币": "DOGE", "瑞波": "XRP",
    "索拉纳": "SOL", "币安币": "BNB", "莱特币": "LTC", "波场": "TRX", "艾达币": "ADA",
}

# 与美股代码可能冲突时，这些默认按加密处理（想查同名股票请用 stock:XXX）
COMMON_CRYPTO = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX", "SHIB", "DOT",
    "LINK", "BCH", "LTC", "NEAR", "UNI", "APT", "SUI", "PEPE", "ARB", "OP", "ATOM", "FIL",
    "INJ", "TIA", "SEI", "WIF", "BONK", "AAVE", "MKR", "LDO", "RNDR", "RENDER", "FET", "TAO",
    "ENA", "ONDO", "JUP", "PYTH", "HYPE", "WLD", "ETC", "XLM", "HBAR", "ICP", "STX", "IMX",
    "CRV", "PENDLE", "ORDI", "TRUMP", "POL", "MATIC", "ALGO", "XMR", "KAS", "BERA", "VIRTUAL",
}


def _crypto(base: str) -> dict[str, Any]:
    base = base.upper()
    return {
        "symbol": base,
        "asset_type": "crypto",
        "spot_pair": f"{base}/USDT",
        "perp": f"{base}/USDT:USDT",
        "name": base,
        "benchmark": "BTC" if base != "BTC" else None,
    }


def _stock(ticker: str, info: dict[str, Any] | None = None) -> dict[str, Any]:
    info = info or {}
    qt = (info.get("quoteType") or "EQUITY").upper()
    return {
        "symbol": ticker.upper(),
        "asset_type": "stock",
        "quote_type": qt,  # EQUITY / ETF / INDEX / MUTUALFUND ...
        "name": info.get("shortName") or info.get("longName") or ticker.upper(),
        "exchange": info.get("exchange"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "benchmark": "SPY" if ticker.upper() != "SPY" else None,
    }


@cached(ttl=7 * 86400)
def _yf_validate(ticker: str) -> dict[str, Any] | None:
    import yfinance as yf

    from .data.us_stock import yfx

    try:
        h = yfx(lambda: yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False))
        if h is None or h.empty:
            return None
    except Exception:
        return None
    info: dict[str, Any] = {}
    try:
        raw = yfx(lambda: yf.Ticker(ticker).info, retries=2) or {}
        info = {k: raw.get(k) for k in ("quoteType", "shortName", "longName", "exchange", "sector", "industry")}
    except Exception:
        pass
    if not info.get("quoteType"):  # info 没取到（限流）：结果可用，但不要缓存 7 天
        info["_nocache"] = True
    return info


@cached(ttl=7 * 86400)
def _yf_search(query: str) -> list[dict[str, Any]]:
    import yfinance as yf

    from .data.us_stock import yfx

    try:
        quotes = yfx(lambda: yf.Search(query, max_results=8).quotes)
    except Exception:
        return []
    return [
        {"symbol": q.get("symbol"), "type": q.get("quoteType"), "name": q.get("shortname") or q.get("longname"),
         "exchange": q.get("exchange")}
        for q in quotes
        if q.get("quoteType") in ("EQUITY", "ETF", "INDEX", "CRYPTOCURRENCY", "MUTUALFUND")
    ]


def _exchange_has(base: str) -> bool:
    from .data import crypto

    try:
        return f"{base.upper()}/USDT" in crypto.markets()
    except Exception:
        return False


def resolve(query: str) -> dict[str, Any]:
    q = query.strip()
    if not q:
        raise ValueError("空的标的代码")
    low = q.lower()

    if low.startswith("crypto:"):
        return _crypto(q.split(":", 1)[1].strip())
    if low.startswith("stock:"):
        t = q.split(":", 1)[1].strip().upper()
        return _stock(t, _yf_validate(t))

    m = re.match(r"^([A-Za-z0-9]+)\s*[-/]\s*(USD|USDT|USDC)$", q, re.I)
    if m:
        return _crypto(m.group(1))

    if q in CN_CRYPTO_ALIASES:
        return _crypto(CN_CRYPTO_ALIASES[q])
    if q in CN_STOCK_ALIASES or low in CN_STOCK_ALIASES:
        t = CN_STOCK_ALIASES.get(q) or CN_STOCK_ALIASES[low]
        return _stock(t, _yf_validate(t))

    up = q.upper()
    if up in COMMON_CRYPTO:
        return _crypto(up)

    if re.match(r"^[\^A-Za-z0-9.\-=]{1,12}$", q):
        info = _yf_validate(up)
        if info is not None and (info.get("quoteType") or "").upper() != "CRYPTOCURRENCY":
            return _stock(up, info)
        if _exchange_has(up):
            return _crypto(up)

    results = _yf_search(q)
    for r in results:
        if r["type"] == "CRYPTOCURRENCY" and r["symbol"]:
            base = r["symbol"].split("-")[0]
            if _exchange_has(base):
                return _crypto(base)
        elif r["symbol"]:
            info = _yf_validate(r["symbol"])
            if info is not None:
                return _stock(r["symbol"], info)
    raise ValueError(f"无法识别标的: {query}。可以试试英文代码，或加前缀 stock:XXX / crypto:XXX")


def search(query: str) -> list[dict[str, Any]]:
    """给 AI 用的模糊搜索。"""
    return _yf_search(query)
