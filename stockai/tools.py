"""暴露给 AI 的工具。每个工具：名称、描述、JSON Schema、实现函数。

设计原则：
- 所有工具接受原始代码（"NVDA" / "BTC" / "苹果"），内部自动解析
- 返回浓缩后的结构化数据，带来源和时间，避免大段原始数据浪费 token
- 出错时返回 {"error": ...}，让 AI 知道缺了什么，而不是编造
"""
from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import symbols
from .analytics import technicals as ta
from .analytics import valuation as val
from .data import crypto, us_stock
from .util import clean, num, pct

SECTOR_ETF = {
    "Technology": "XLK", "Healthcare": "XLV", "Financial Services": "XLF", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Energy": "XLE", "Industrials": "XLI", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Utilities": "XLU", "Communication Services": "XLC",
}

_CRYPTO_BARS = {"15m": 400, "30m": 400, "1h": 500, "4h": 500, "1d": 500, "1wk": 260}


def ohlcv(sym: dict[str, Any], interval: str) -> Any:
    if sym["asset_type"] == "crypto":
        return crypto.history(sym["symbol"], interval, _CRYPTO_BARS.get(interval, 500))
    return us_stock.history(sym["symbol"], interval)


# ---------------------------------------------------------------- 工具实现
def t_resolve_symbol(query: str) -> dict[str, Any]:
    try:
        return {"resolved": symbols.resolve(query)}
    except ValueError as e:
        return {"error": str(e), "candidates": symbols.search(query)}


def t_get_quote(symbol: str) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    q = crypto.quote(s["symbol"]) if s["asset_type"] == "crypto" else us_stock.quote(s["symbol"])
    return {"asset": s, "quote": q}


def t_get_technicals(symbol: str, timeframes: list[str] | None = None) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    tfs = timeframes or ["1h", "4h", "1d"]
    tfs = [t for t in tfs if t in ("15m", "30m", "1h", "4h", "1d", "1wk")] or ["1d"]
    out: dict[str, Any] = {"symbol": s["symbol"], "asset_type": s["asset_type"], "timeframes": {}}

    def one(tf: str):
        try:
            return tf, ta.summarize(ohlcv(s, tf), tf)
        except Exception as e:
            return tf, {"error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=4) as ex:
        for tf, res in ex.map(one, tfs):
            out["timeframes"][tf] = res
    out["mtf_alignment"] = ta.mtf_alignment([out["timeframes"][t] for t in tfs])

    # 日线收益/风险 + 相对强弱
    try:
        daily = ohlcv(s, "1d")
        bench_sym = s.get("benchmark")
        bench = None
        if bench_sym:
            bench = ohlcv(symbols.resolve(bench_sym), "1d")
        ppy = 365 if s["asset_type"] == "crypto" else 252
        perf = ta.performance(daily, bench, ppy)
        perf["benchmark"] = bench_sym
        if s["asset_type"] == "stock" and s.get("sector") in SECTOR_ETF:
            etf = SECTOR_ETF[s["sector"]]
            sp = ta.performance(daily, us_stock.history(etf, "1d"), 252)
            perf["vs_sector_etf"] = {"etf": etf, "excess_%": sp.get("excess_return_vs_benchmark_%")}
        out["performance_daily"] = perf
    except Exception as e:
        out["performance_daily"] = {"error": str(e)[:200]}
    return clean(out)


def t_get_fundamentals(symbol: str) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    if s["asset_type"] == "crypto":
        out: dict[str, Any] = {"symbol": s["symbol"], "asset_type": "crypto"}
        for k, fn in (("market", crypto.coingecko), ("onchain_defi", crypto.defillama)):
            try:
                out[k] = fn(s["symbol"])
            except Exception as e:
                out[k] = {"error": str(e)[:200]}
        return out
    if s.get("quote_type") in ("ETF", "INDEX", "MUTUALFUND"):
        i = us_stock.info(s["symbol"])
        return clean({"symbol": s["symbol"], "type": s.get("quote_type"), "name": i.get("longName"),
                      "category": i.get("category"), "total_assets": i.get("totalAssets"),
                      "expense_ratio": i.get("annualReportExpenseRatio") or i.get("netExpenseRatio"),
                      "ytd_return": i.get("ytdReturn"), "pe_ttm": num(i.get("trailingPE")),
                      "summary": (i.get("longBusinessSummary") or "")[:400],
                      "note": "ETF/指数没有公司财报，基本面请看成分股"})
    return us_stock.fundamentals(s["symbol"])


def t_get_long_term_financials(symbol: str) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    if s["asset_type"] != "stock":
        return {"error": "仅适用于美股公司"}
    from .data import sec

    out = sec.long_history(s["symbol"])
    hist = [h["pe"] for h in out.get("historical_pe_by_fy", []) if h.get("pe") and h["pe"] > 0]
    cur = us_stock.info(s["symbol"]).get("trailingPE")
    if hist and cur:
        out["current_pe_ttm"] = num(cur)
        out["current_pe_percentile_vs_history"] = num(sum(h <= cur for h in hist) / len(hist), 3)
        out["historical_pe_median"] = num(sorted(hist)[len(hist) // 2])
    return out


def t_get_derivatives(symbol: str) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    if s["asset_type"] == "crypto":
        return crypto.derivatives(s["symbol"])
    return us_stock.options_summary(s["symbol"])


def t_get_news(symbol: str, limit: int = 10) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    yf_sym = f"{s['symbol']}-USD" if s["asset_type"] == "crypto" else s["symbol"]
    return {"symbol": s["symbol"], "news": us_stock.news(yf_sym, min(int(limit), 20)),
            "note": "仅标题与摘要，来自 Yahoo Finance"}


def t_get_events(symbol: str) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    if s["asset_type"] == "crypto":
        return {"error": "加密货币暂无财报/内部人数据，可用 get_news 看事件"}
    return us_stock.events(s["symbol"])


def t_get_peers(symbol: str, peers: list[str] | None = None) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    if s["asset_type"] == "stock":
        return us_stock.peer_compare(s["symbol"], peers)
    rows = []
    for b in [s["symbol"]] + [p.upper() for p in (peers or ["BTC", "ETH", "SOL"]) if p.upper() != s["symbol"]][:6]:
        try:
            cg = crypto.coingecko(b)
            rows.append({"symbol": b, **{k: cg.get(k) for k in ("market_cap", "market_cap_rank", "mcap_to_fdv",
                                                                 "change_30d_%", "change_1y_%", "from_ath_%")}})
        except Exception as e:
            rows.append({"symbol": b, "error": str(e)[:100]})
    return clean({"peers": rows})


def _brief(sym: str, interval: str = "1d") -> dict[str, Any]:
    s = symbols.resolve(sym)
    df = ohlcv(s, interval)
    c = df["Close"]
    out = {"price": num(c.iloc[-1]), "chg_1d_%": pct(c.iloc[-1] / c.iloc[-2] - 1),
           "chg_1m_%": pct(c.iloc[-1] / c.iloc[-22] - 1) if len(c) > 22 else None,
           "chg_3m_%": pct(c.iloc[-1] / c.iloc[-64] - 1) if len(c) > 64 else None}
    if len(c) >= 200:
        out["above_ma50"] = bool(c.iloc[-1] > c.rolling(50).mean().iloc[-1])
        out["above_ma200"] = bool(c.iloc[-1] > c.rolling(200).mean().iloc[-1])
    return out


def t_get_market_context(asset_type: str = "stock") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if asset_type == "crypto":
        for k, fn in (("BTC", lambda: _brief("BTC")), ("ETH", lambda: _brief("ETH")),
                      ("fear_greed", crypto.fear_greed), ("global", crypto.global_market),
                      ("btc_derivatives", lambda: {k: v for k, v in crypto.derivatives("BTC").items()
                                                   if k in ("funding_rate_%", "funding_avg_7d_%", "oi_change_7d_%",
                                                            "long_short_account_ratio")})):
            try:
                out[k] = fn()
            except Exception as e:
                out[k] = {"error": str(e)[:150]}
        return clean(out)
    names = {"SPY": "标普500", "QQQ": "纳指100", "IWM": "罗素2000", "^VIX": "VIX恐慌指数",
             "^TNX": "10年美债收益率(x10)", "DX-Y.NYB": "美元指数", "GLD": "黄金", "BTC": "比特币"}

    def one(sym):
        try:
            return sym, {"name": names[sym], **_brief(f"stock:{sym}" if sym != "BTC" else "BTC")}
        except Exception as e:
            return sym, {"name": names[sym], "error": str(e)[:100]}

    with ThreadPoolExecutor(max_workers=6) as ex:
        out["indices"] = dict(ex.map(one, names))

    def sec_one(item):
        sector, etf = item
        try:
            return sector, {"etf": etf, **_brief(f"stock:{etf}")}
        except Exception:
            return sector, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        sectors = {k: v for k, v in ex.map(sec_one, SECTOR_ETF.items()) if v}
    out["sectors_by_1m"] = dict(sorted(sectors.items(), key=lambda kv: -(kv[1].get("chg_1m_%") or -999)))
    return clean(out)


def t_run_dcf(symbol: str, scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    if s["asset_type"] != "stock":
        return {"error": "DCF 仅适用于有自由现金流的公司"}
    i = us_stock.info(s["symbol"])
    fcf = i.get("freeCashflow")
    shares = i.get("sharesOutstanding") or i.get("impliedSharesOutstanding")
    net_cash = (i.get("totalCash") or 0) - (i.get("totalDebt") or 0)
    price = i.get("currentPrice") or i.get("regularMarketPrice")
    scenarios = scenarios or [
        {"name": "悲观", "growth_5y": 0.05, "terminal_growth": 0.02, "discount_rate": 0.10},
        {"name": "中性", "growth_5y": 0.10, "terminal_growth": 0.03, "discount_rate": 0.09},
        {"name": "乐观", "growth_5y": 0.18, "terminal_growth": 0.03, "discount_rate": 0.085},
    ]
    return {"dcf": val.dcf(fcf, shares, net_cash, price, scenarios),
            "reverse_dcf": val.reverse_dcf(fcf, shares, net_cash, price)}


def t_trade_plan(symbol: str, entry: float, stop: float, targets: list[float],
                 account_size: float | None = None, risk_pct: float = 1.0) -> dict[str, Any]:
    s = symbols.resolve(symbol)
    try:
        a = float(ta.atr(ohlcv(s, "1d")).iloc[-1])
    except Exception:
        a = None
    plan = val.trade_plan(float(entry), float(stop), [float(t) for t in targets], a, account_size, risk_pct)
    plan["daily_atr"] = num(a) if a else None
    return plan


# ---------------------------------------------------------------- 注册表
SYMBOL_PROP = {"type": "string", "description": "标的代码，如 NVDA、AAPL、SPY、BTC、ETH、SOL；也接受中文名或 stock:XXX / crypto:XXX"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "resolve_symbol",
        "description": "把用户输入（代码/中文名/英文公司名）解析成标准标的，判断是美股还是加密货币。名称不确定时先调用它。",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "fn": t_resolve_symbol,
    },
    {
        "name": "get_quote",
        "description": "最新报价：价格、涨跌幅、日内区间、52周区间、成交量对比、市值、盘前盘后。",
        "input_schema": {"type": "object", "properties": {"symbol": SYMBOL_PROP}, "required": ["symbol"]},
        "fn": t_get_quote,
    },
    {
        "name": "get_technicals",
        "description": ("多周期技术面摘要（代码计算）：均线/EMA21、RSI、MACD、ADX、布林带、ATR、量比、OBV、"
                        "支撑压力位（含触及次数）、成交量分布 POC、最近K线、多周期共振判断；"
                        "以及日线收益、最大回撤、波动率、相对基准(SPY/BTC)与行业ETF的超额收益、Beta。"
                        "短线常用 ['15m','1h','4h','1d']，长线用 ['1d','1wk']。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL_PROP,
                "timeframes": {"type": "array", "items": {"type": "string", "enum": ["15m", "30m", "1h", "4h", "1d", "1wk"]},
                               "description": "周期列表，默认 ['1h','4h','1d']"},
            },
            "required": ["symbol"],
        },
        "fn": t_get_technicals,
    },
    {
        "name": "get_fundamentals",
        "description": ("基本面。美股：估值(PE/PS/PB/EV-EBITDA/FCF收益率)、盈利能力、增长、资产负债、空头比例、"
                        "分析师目标价、近5年年报与近6季财务、Piotroski F分与Altman Z分。"
                        "加密：市值/FDV/流通比例/ATH、所属板块，以及 DefiLlama 的 TVL、手续费、协议收入、市值/收入比。"),
        "input_schema": {"type": "object", "properties": {"symbol": SYMBOL_PROP}, "required": ["symbol"]},
        "fn": t_get_fundamentals,
    },
    {
        "name": "get_long_term_financials",
        "description": "仅美股：SEC 官方年报 10 年以上的营收、净利润、经营利润、EPS、经营现金流、FCF，3/5/10年复合增速，以及各财年历史PE（用于判断当前估值分位）。长线分析用。",
        "input_schema": {"type": "object", "properties": {"symbol": SYMBOL_PROP}, "required": ["symbol"]},
        "fn": t_get_long_term_financials,
    },
    {
        "name": "get_derivatives",
        "description": ("衍生品数据。美股：近月和约30天期权的 ATM 隐含波动率、隐含波动幅度、Put/Call 比、最大痛点、持仓最大的行权价。"
                        "加密：永续资金费率(当前/7日/30日均值)、持仓量及24h/7d变化、多空账户比、主动买卖比、盘口1%深度。"),
        "input_schema": {"type": "object", "properties": {"symbol": SYMBOL_PROP}, "required": ["symbol"]},
        "fn": t_get_derivatives,
    },
    {
        "name": "get_news",
        "description": "近期新闻标题与摘要（Yahoo Finance）。",
        "input_schema": {"type": "object", "properties": {"symbol": SYMBOL_PROP, "limit": {"type": "integer", "default": 10}},
                         "required": ["symbol"]},
        "fn": t_get_news,
    },
    {
        "name": "get_events",
        "description": "仅美股：下次财报日期与距今天数、EPS/营收一致预期、最近4次财报超预期情况、分析师评级变动、近6个月内部人买卖、主要机构持仓变化。",
        "input_schema": {"type": "object", "properties": {"symbol": SYMBOL_PROP}, "required": ["symbol"]},
        "fn": t_get_events,
    },
    {
        "name": "get_peers",
        "description": "同行对比。美股自动找同行业市值靠前的公司比较估值和盈利能力（也可指定 peers）；加密默认和 BTC/ETH/SOL 比较。",
        "input_schema": {"type": "object", "properties": {
            "symbol": SYMBOL_PROP,
            "peers": {"type": "array", "items": {"type": "string"}, "description": "可选，自定义对比列表"}},
            "required": ["symbol"]},
        "fn": t_get_peers,
    },
    {
        "name": "get_market_context",
        "description": ("大盘环境。stock：标普/纳指/罗素/VIX/美债收益率/美元/黄金/BTC 的涨跌和均线位置，以及11个行业ETF近1月强弱排序。"
                        "crypto：BTC/ETH 走势、恐惧贪婪指数、总市值与BTC市占率、BTC资金费率与持仓变化。"),
        "input_schema": {"type": "object", "properties": {"asset_type": {"type": "string", "enum": ["stock", "crypto"]}},
                         "required": ["asset_type"]},
        "fn": t_get_market_context,
    },
    {
        "name": "run_dcf",
        "description": ("仅美股：用 TTM 自由现金流做两阶段 DCF，可传多个情景假设（小数形式，如 0.12 表示 12%）；"
                        "同时返回反向DCF——当前股价隐含的未来5年FCF增速。不传 scenarios 则用默认悲观/中性/乐观。"),
        "input_schema": {"type": "object", "properties": {
            "symbol": SYMBOL_PROP,
            "scenarios": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "growth_5y": {"type": "number", "description": "1-5年FCF年增速，小数"},
                "terminal_growth": {"type": "number", "description": "永续增速，小数，通常 0.02-0.035"},
                "discount_rate": {"type": "number", "description": "折现率，小数，通常 0.08-0.12"}},
                "required": ["growth_5y", "terminal_growth", "discount_rate"]}}},
            "required": ["symbol"]},
        "fn": t_run_dcf,
    },
    {
        "name": "trade_plan",
        "description": "计算交易计划：止损距离(%和ATR倍数)、各目标价的盈亏比(R倍数)；给出账户规模时按风险比例算仓位。短线给出入场/止损/目标后务必调用它核算。",
        "input_schema": {"type": "object", "properties": {
            "symbol": SYMBOL_PROP,
            "entry": {"type": "number"}, "stop": {"type": "number"},
            "targets": {"type": "array", "items": {"type": "number"}},
            "account_size": {"type": "number", "description": "可选，账户资金（美元）"},
            "risk_pct": {"type": "number", "description": "单笔风险占账户百分比，默认 1"}},
            "required": ["symbol", "entry", "stop", "targets"]},
        "fn": t_trade_plan,
    },
]

_BY_NAME: dict[str, Callable] = {t["name"]: t["fn"] for t in TOOLS}


def tool_specs() -> list[dict[str, Any]]:
    """不含实现函数的工具定义（name/description/input_schema）。"""
    return [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]


def call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """执行工具，返回 (JSON 字符串, 是否出错)。"""
    fn = _BY_NAME.get(name)
    if fn is None:
        return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False), True
    try:
        res = fn(**(args or {}))
        return json.dumps(clean(res), ensure_ascii=False, default=str), False
    except TypeError as e:
        return json.dumps({"error": f"参数错误: {e}"}, ensure_ascii=False), True
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {str(e)[:300]}",
                           "trace": traceback.format_exc(limit=2)[-300:]}, ensure_ascii=False), True
