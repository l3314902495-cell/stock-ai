"""加密货币数据：
- ccxt（默认 OKX 公开接口，免 key）：行情、K 线、资金费率、持仓量、多空比、盘口
- CoinGecko 免费接口：市值、供应量、FDV、ATH
- DefiLlama：协议 TVL / 手续费 / 收入（币的"基本面"）
- alternative.me：恐惧贪婪指数
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import ccxt
import pandas as pd
import requests

from ..cache import cached
from ..config import data_proxy, get_config
from ..util import big, clean, num, pct

_ex = None
_ex_lock = threading.Lock()


def exchange() -> ccxt.Exchange:
    global _ex
    with _ex_lock:
        if _ex is None:
            name = get_config()["data"].get("crypto_exchange", "okx")
            opts: dict[str, Any] = {"enableRateLimit": True, "timeout": 20000}
            proxy = data_proxy()
            if proxy:
                opts["https_proxy"] = proxy  # ccxt 不会自动读取 HTTPS_PROXY 环境变量
            _ex = getattr(ccxt, name)(opts)
        return _ex


def _http(url: str, params: dict | None = None) -> Any:
    proxy = data_proxy()
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "stock-ai/0.1"},
                     proxies={"https": proxy} if proxy else None)
    r.raise_for_status()
    return r.json()


@cached(ttl=86400)
def markets() -> list[str]:
    ex = exchange()
    ex.load_markets()
    return list(ex.symbols)


def _ensure_markets() -> None:
    ex = exchange()
    if not ex.markets:
        ex.load_markets()


# ---------------------------------------------------------------- 行情
_TF = {"15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1wk": "1w"}
_TTL = {"15m": 120, "30m": 180, "1h": 300, "4h": 600, "1d": 1200, "1wk": 3600}


def history(base: str, interval: str = "1d", limit: int = 500) -> pd.DataFrame:
    @cached(ttl=_TTL.get(interval, 600))
    def _load(base: str, interval: str, limit: int) -> pd.DataFrame:
        _ensure_markets()
        ex = exchange()
        tf = _TF.get(interval, interval)
        since = ex.milliseconds() - limit * ex.parse_timeframe(tf) * 1000
        # 交易所单次返回条数有限，用 ccxt 自带分页
        rows = ex.fetch_ohlcv(f"{base}/USDT", tf, since=since, limit=limit,
                              params={"paginate": True, "paginationCalls": limit // 100 + 3})
        if not rows:
            raise ValueError(f"{base}/USDT 没有 {interval} K 线")
        df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]).drop_duplicates("ts")
        df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.drop(columns="ts").sort_index()

    return _load(base.upper(), interval, limit)


@cached(ttl=30)
def _ticker(pair: str) -> dict:
    _ensure_markets()
    return exchange().fetch_ticker(pair)


def quote(base: str) -> dict[str, Any]:
    base = base.upper()
    t = _ticker(f"{base}/USDT")
    out = {
        "symbol": base,
        "pair": f"{base}/USDT",
        "exchange": exchange().id,
        "price": num(t.get("last")),
        "change_24h_pct": round(t["percentage"], 2) if t.get("percentage") is not None else None,
        "high_24h": num(t.get("high")),
        "low_24h": num(t.get("low")),
        "volume_24h_usdt": big(t.get("quoteVolume")),
        "bid": num(t.get("bid")),
        "ask": num(t.get("ask")),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    try:
        cg = coingecko(base)
        out.update({k: cg.get(k) for k in ("market_cap", "market_cap_rank", "fdv")})
    except Exception:
        pass
    return clean(out)


# ---------------------------------------------------------------- 衍生品
def _okx_ccy_stat(endpoint: str, base: str, period: str = "1H") -> list:
    ex = exchange()
    if ex.id != "okx":
        return []
    fn = getattr(ex, endpoint)
    return fn({"ccy": base, "period": period}).get("data", [])


@cached(ttl=300)
def derivatives(base: str) -> dict[str, Any]:
    base = base.upper()
    _ensure_markets()
    ex = exchange()
    perp = f"{base}/USDT:USDT"
    out: dict[str, Any] = {"symbol": base, "perp": perp, "exchange": ex.id}
    if perp not in ex.markets:
        out["note"] = f"{ex.id} 没有 {perp} 永续合约"
        return out
    try:
        fr = ex.fetch_funding_rate(perp)
        out["funding_rate_%"] = pct(fr.get("fundingRate"), 4)
        out["next_funding_time"] = fr.get("fundingDatetime")
        out["funding_annualized_%"] = pct((fr.get("fundingRate") or 0) * 3 * 365, 1)
    except Exception as e:
        out["funding_error"] = str(e)[:120]
    try:
        hist = ex.fetch_funding_rate_history(perp, limit=90)
        rates = [h["fundingRate"] for h in hist if h.get("fundingRate") is not None]
        if rates:
            out["funding_avg_7d_%"] = pct(sum(rates[-21:]) / len(rates[-21:]), 4)
            out["funding_avg_30d_%"] = pct(sum(rates) / len(rates), 4)
            out["funding_positive_ratio_30d"] = num(sum(r > 0 for r in rates) / len(rates))
    except Exception:
        pass
    try:
        oi = ex.fetch_open_interest(perp)
        out["open_interest_usd"] = big(oi.get("openInterestValue"))
    except Exception:
        pass
    try:
        # OKX 的持仓量与成交量历史（按币种汇总全部合约）
        rows = _okx_ccy_stat("publicGetRubikStatContractsOpenInterestVolume", base, "1H")
        if rows:
            ois = [float(r[1]) for r in rows]  # [ts, oi(USD), vol(USD)]
            if len(ois) > 24:
                out["oi_change_24h_%"] = pct(ois[0] / ois[24] - 1)
            if len(ois) > 24 * 7:
                out["oi_change_7d_%"] = pct(ois[0] / ois[24 * 7] - 1)
        ls = _okx_ccy_stat("publicGetRubikStatContractsLongShortAccountRatio", base, "1H")
        if ls:
            out["long_short_account_ratio"] = num(float(ls[0][1]))
            if len(ls) > 24:
                out["long_short_ratio_24h_ago"] = num(float(ls[24][1]))
        tv = _okx_ccy_stat("publicGetRubikStatTakerVolume", base, "1H")
        if tv:
            # [ts, sellVol, buyVol]
            sell = sum(float(r[1]) for r in tv[:24])
            buy = sum(float(r[2]) for r in tv[:24])
            if sell:
                out["taker_buy_sell_ratio_24h"] = num(buy / sell)
    except Exception:
        pass
    try:
        ob = ex.fetch_order_book(f"{base}/USDT", limit=100)
        mid = (ob["bids"][0][0] + ob["asks"][0][0]) / 2
        bid_d = sum(p * a for p, a, *_ in ob["bids"] if p >= mid * 0.99)
        ask_d = sum(p * a for p, a, *_ in ob["asks"] if p <= mid * 1.01)
        out["orderbook_1pct_depth"] = {"bid_usd": big(bid_d), "ask_usd": big(ask_d),
                                       "bid_ask_ratio": num(bid_d / ask_d) if ask_d else None}
    except Exception:
        pass
    out["source"] = f"{ex.id} 公开接口"
    return clean(out)


# ---------------------------------------------------------------- 基本面
@cached(ttl=7 * 86400)
def _cg_id(base: str) -> str | None:
    res = _http("https://api.coingecko.com/api/v3/search", {"query": base})
    coins = [c for c in res.get("coins", []) if (c.get("symbol") or "").upper() == base.upper()]
    if not coins:
        return None
    coins.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
    return coins[0]["id"]


@cached(ttl=1800)
def coingecko(base: str) -> dict[str, Any]:
    cid = _cg_id(base.upper())
    if not cid:
        return {"note": f"CoinGecko 找不到 {base}"}
    d = _http(f"https://api.coingecko.com/api/v3/coins/{cid}",
              {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false"})
    md = d.get("market_data", {})
    usd = lambda k: (md.get(k) or {}).get("usd")  # noqa: E731
    circ, total, mx = md.get("circulating_supply"), md.get("total_supply"), md.get("max_supply")
    return clean({
        "coingecko_id": cid,
        "name": d.get("name"),
        "categories": (d.get("categories") or [])[:6],
        "description": ((d.get("description") or {}).get("en") or "")[:500],
        "market_cap": big(usd("market_cap")),
        "market_cap_rank": d.get("market_cap_rank"),
        "fdv": big(usd("fully_diluted_valuation")),
        "mcap_to_fdv": num(usd("market_cap") / usd("fully_diluted_valuation")) if usd("market_cap") and usd("fully_diluted_valuation") else None,
        "circulating_supply": big(circ),
        "total_supply": big(total),
        "max_supply": big(mx),
        "circulating_pct_of_max": pct(circ / mx) if circ and mx else None,
        "ath": num(usd("ath")),
        "ath_date": ((md.get("ath_date") or {}).get("usd") or "")[:10] or None,
        "from_ath_%": num((md.get("ath_change_percentage") or {}).get("usd")),
        "change_7d_%": num(md.get("price_change_percentage_7d")),
        "change_30d_%": num(md.get("price_change_percentage_30d")),
        "change_1y_%": num(md.get("price_change_percentage_1y")),
        "genesis_date": d.get("genesis_date"),
        "source": "CoinGecko",
    })


@cached(ttl=86400)
def _llama_protocols() -> list[dict]:
    return _http("https://api.llama.fi/protocols")


@cached(ttl=86400)
def _llama_chains() -> list[dict]:
    return _http("https://api.llama.fi/v2/chains")


@cached(ttl=3600)
def defillama(base: str) -> dict[str, Any]:
    base = base.upper()
    out: dict[str, Any] = {"symbol": base}
    try:
        chains = [c for c in _llama_chains() if (c.get("tokenSymbol") or "").upper() == base]
        if chains:
            c = max(chains, key=lambda x: x.get("tvl") or 0)
            out["chain"] = {"name": c.get("name"), "tvl": big(c.get("tvl"))}
            try:
                fees = _http(f"https://api.llama.fi/summary/fees/{c['name']}", {"dataType": "dailyFees"})
                out["chain"]["fees_24h"] = big(fees.get("total24h"))
                out["chain"]["fees_30d"] = big(fees.get("total30d"))
                out["chain"]["fees_change_30d_%"] = num(fees.get("change_1m"))
            except Exception:
                pass
    except Exception:
        pass
    try:
        prots = [p for p in _llama_protocols() if (p.get("symbol") or "").upper() == base]
        if prots:
            p = max(prots, key=lambda x: x.get("tvl") or 0)
            slug = p.get("slug")
            out["protocol"] = {
                "name": p.get("name"), "category": p.get("category"), "chains": (p.get("chains") or [])[:6],
                "tvl": big(p.get("tvl")), "tvl_change_1d_%": num(p.get("change_1d")),
                "tvl_change_7d_%": num(p.get("change_7d")),
                "mcap_to_tvl": num(p["mcap"] / p["tvl"]) if p.get("mcap") and p.get("tvl") else None,
            }
            for kind, key in (("dailyFees", "fees"), ("dailyRevenue", "revenue")):
                try:
                    f = _http(f"https://api.llama.fi/summary/fees/{slug}", {"dataType": kind})
                    out["protocol"][f"{key}_24h"] = big(f.get("total24h"))
                    out["protocol"][f"{key}_30d"] = big(f.get("total30d"))
                    out["protocol"][f"{key}_1y"] = big(f.get("total1y"))
                    if key == "revenue" and f.get("total30d") and p.get("mcap"):
                        out["protocol"]["mcap_to_annualized_revenue"] = num(p["mcap"] / (f["total30d"] * 12.17))
                except Exception:
                    pass
    except Exception:
        pass
    if len(out) == 1:
        out["note"] = "DefiLlama 上没有该代币对应的链或协议数据（纯 meme 币等没有基本面数据很正常）"
    out["source"] = "DefiLlama"
    return clean(out)


@cached(ttl=3600)
def fear_greed() -> dict[str, Any]:
    d = _http("https://api.alternative.me/fng/", {"limit": 30})["data"]
    vals = [int(x["value"]) for x in d]
    return {"today": vals[0], "label": d[0]["value_classification"], "7d_ago": vals[7] if len(vals) > 7 else None,
            "30d_avg": round(sum(vals) / len(vals), 1), "source": "alternative.me"}


@cached(ttl=1800)
def global_market() -> dict[str, Any]:
    d = _http("https://api.coingecko.com/api/v3/global")["data"]
    return clean({
        "total_market_cap": big(d["total_market_cap"]["usd"]),
        "market_cap_change_24h_%": num(d.get("market_cap_change_percentage_24h_usd")),
        "btc_dominance_%": num(d["market_cap_percentage"].get("btc")),
        "eth_dominance_%": num(d["market_cap_percentage"].get("eth")),
    })
