"""美股/ETF/指数数据：yfinance（免费，非官方接口——务必缓存）。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from ..cache import cached
from ..util import big, clean, num, pct

# yfinance 各周期能取到的最长历史
_MAX_PERIOD = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "max", "1wk": "max"}
_TTL = {"1m": 60, "5m": 120, "15m": 300, "30m": 300, "1h": 600, "4h": 600, "1d": 1800, "1wk": 3600}


def _tk(t: str) -> yf.Ticker:
    return yf.Ticker(t)


def _history_raw(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = _tk(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"{ticker} 没有 {interval} 行情数据")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return df


def history(ticker: str, interval: str = "1d", period: str | None = None) -> pd.DataFrame:
    """统一 OHLCV。interval: 15m/30m/1h/4h/1d/1wk。4h 由 1h 重采样。"""
    key_interval = interval

    @cached(ttl=_TTL.get(interval, 600))
    def _load(ticker: str, interval: str, period: str | None) -> pd.DataFrame:
        if interval == "4h":
            df = _history_raw(ticker, "1h", period or "730d")
            agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
            # 按交易日内 4 小时切分（美股 9:30 开盘：9:30-13:30, 13:30-16:00）
            df = df.resample("4h", offset="1h30min").agg(agg).dropna(subset=["Close"])
            return df
        p = period or {"1d": "5y", "1wk": "max"}.get(interval) or _MAX_PERIOD.get(interval, "1y")
        return _history_raw(ticker, interval, p)

    return _load(ticker.upper(), key_interval, period)


@cached(ttl=60)
def _fast(ticker: str) -> dict[str, Any]:
    fi = _tk(ticker).fast_info
    out = {}
    for k in ("last_price", "previous_close", "open", "day_high", "day_low", "last_volume",
              "ten_day_average_volume", "three_month_average_volume", "year_high", "year_low",
              "market_cap", "fifty_day_average", "two_hundred_day_average", "currency", "exchange"):
        try:
            out[k] = fi[k]
        except Exception:
            out[k] = None
    return out


@cached(ttl=1800)
def info(ticker: str) -> dict[str, Any]:
    try:
        return _tk(ticker).info or {}
    except Exception:
        return {}


def quote(ticker: str) -> dict[str, Any]:
    f = _fast(ticker)
    i = info(ticker)
    last, prev = f.get("last_price"), f.get("previous_close")
    chg = (last / prev - 1) if last and prev else None
    out = {
        "symbol": ticker.upper(),
        "name": i.get("shortName"),
        "type": i.get("quoteType"),
        "currency": f.get("currency"),
        "price": num(last),
        "change_pct": pct(chg),
        "open": num(f.get("open")),
        "day_range": [num(f.get("day_low")), num(f.get("day_high"))],
        "52w_range": [num(f.get("year_low")), num(f.get("year_high"))],
        "from_52w_high_pct": pct(last / f["year_high"] - 1) if last and f.get("year_high") else None,
        "volume": big(f.get("last_volume")),
        "volume_vs_3m_avg": num(f["last_volume"] / f["three_month_average_volume"])
        if f.get("last_volume") and f.get("three_month_average_volume") else None,
        "market_cap": big(f.get("market_cap")),
        "pre_market": num(i.get("preMarketPrice")),
        "post_market": num(i.get("postMarketPrice")),
        "market_state": i.get("marketState"),
        "sector": i.get("sector"),
        "industry": i.get("industry"),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "yfinance",
    }
    return clean(out)


# ---------------------------------------------------------------- 基本面
def _row(df: pd.DataFrame, *names: str) -> pd.Series | None:
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def _series_table(df: pd.DataFrame, rows: dict[str, tuple[str, ...]], n: int) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = sorted(df.columns, reverse=True)[:n]
    out = []
    for c in sorted(cols):
        rec: dict[str, Any] = {"period": pd.Timestamp(c).strftime("%Y-%m-%d")}
        for label, names in rows.items():
            s = _row(df, *names)
            v = s.get(c) if s is not None else None
            rec[label] = None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
        out.append(rec)
    return out


_IS_ROWS = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "EBIT"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "eps_diluted": ("Diluted EPS",),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
}
_CF_ROWS = {
    "operating_cf": ("Operating Cash Flow",),
    "capex": ("Capital Expenditure",),
    "fcf": ("Free Cash Flow",),
    "buyback": ("Repurchase Of Capital Stock",),
    "sbc": ("Stock Based Compensation",),
}
_BS_ROWS = {
    "total_assets": ("Total Assets",),
    "total_liabilities": ("Total Liabilities Net Minority Interest",),
    "equity": ("Stockholders Equity", "Common Stock Equity"),
    "cash": ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
    "total_debt": ("Total Debt",),
    "current_assets": ("Current Assets",),
    "current_liabilities": ("Current Liabilities",),
    "retained_earnings": ("Retained Earnings",),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "working_capital": ("Working Capital",),
}


@cached(ttl=86400)
def statements(ticker: str) -> dict[str, Any]:
    t = _tk(ticker)
    data: dict[str, Any] = {}
    for name, attr, rows, n in (
        ("income_annual", "income_stmt", _IS_ROWS, 5),
        ("income_quarterly", "quarterly_income_stmt", _IS_ROWS, 6),
        ("cashflow_annual", "cashflow", _CF_ROWS, 5),
        ("cashflow_quarterly", "quarterly_cashflow", _CF_ROWS, 5),
        ("balance_annual", "balance_sheet", _BS_ROWS, 5),
        ("balance_quarterly", "quarterly_balance_sheet", _BS_ROWS, 2),
    ):
        try:
            data[name] = _series_table(getattr(t, attr), rows, n)
        except Exception:
            data[name] = []
    return data


def _growth(rows: list[dict], key: str, lag: int = 1) -> float | None:
    vals = [r.get(key) for r in rows]
    if len(vals) <= lag or vals[-1] is None or vals[-1 - lag] in (None, 0):
        return None
    return vals[-1] / vals[-1 - lag] - 1 if vals[-1 - lag] > 0 else None


def fundamentals(ticker: str) -> dict[str, Any]:
    i = info(ticker)
    st = statements(ticker)
    ia, iq = st["income_annual"], st["income_quarterly"]

    # 季度同比（第 -1 季 vs 第 -5 季）
    q_yoy_rev = _growth(iq, "revenue", 4) if len(iq) >= 5 else None
    q_yoy_eps = _growth(iq, "eps_diluted", 4) if len(iq) >= 5 else None

    def fmt_rows(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            rr = {"period": r["period"]}
            for k, v in r.items():
                if k == "period":
                    continue
                rr[k] = num(v) if k == "eps_diluted" else big(v)
            if r.get("revenue"):
                for m, src in (("gross_margin_%", "gross_profit"), ("op_margin_%", "operating_income"),
                               ("net_margin_%", "net_income")):
                    if r.get(src) is not None:
                        rr[m] = pct(r[src] / r["revenue"])
            out.append(rr)
        return out

    out = {
        "symbol": ticker.upper(),
        "name": i.get("longName") or i.get("shortName"),
        "sector": i.get("sector"),
        "industry": i.get("industry"),
        "business_summary": (i.get("longBusinessSummary") or "")[:600],
        "valuation": {
            "market_cap": big(i.get("marketCap")),
            "enterprise_value": big(i.get("enterpriseValue")),
            "pe_ttm": num(i.get("trailingPE")),
            "pe_forward": num(i.get("forwardPE")),
            "peg": num(i.get("trailingPegRatio") or i.get("pegRatio")),
            "ps_ttm": num(i.get("priceToSalesTrailing12Months")),
            "pb": num(i.get("priceToBook")),
            "ev_ebitda": num(i.get("enterpriseToEbitda")),
            "ev_revenue": num(i.get("enterpriseToRevenue")),
            "fcf_yield_%": pct(i["freeCashflow"] / i["marketCap"]) if i.get("freeCashflow") and i.get("marketCap") else None,
            "dividend_yield_%": num(i.get("dividendYield")),
        },
        "profitability": {
            "gross_margin_%": pct(i.get("grossMargins")),
            "operating_margin_%": pct(i.get("operatingMargins")),
            "net_margin_%": pct(i.get("profitMargins")),
            "roe_%": pct(i.get("returnOnEquity")),
            "roa_%": pct(i.get("returnOnAssets")),
        },
        "growth": {
            "revenue_yoy_latest_q_%": pct(q_yoy_rev if q_yoy_rev is not None else i.get("revenueGrowth")),
            "eps_yoy_latest_q_%": pct(q_yoy_eps if q_yoy_eps is not None else i.get("earningsGrowth")),
            "revenue_growth_last_fy_%": pct(_growth(ia, "revenue")),
            "revenue_cagr_%": pct(_cagr([r.get("revenue") for r in ia])),
            "years_in_cagr": max(len(ia) - 1, 0),
        },
        "balance_sheet": {
            "cash": big(i.get("totalCash")),
            "debt": big(i.get("totalDebt")),
            "net_cash": big((i.get("totalCash") or 0) - (i.get("totalDebt") or 0)) if i.get("totalCash") is not None else None,
            "debt_to_equity": num(i.get("debtToEquity")),
            "current_ratio": num(i.get("currentRatio")),
            "free_cash_flow_ttm": big(i.get("freeCashflow")),
            "operating_cash_flow_ttm": big(i.get("operatingCashflow")),
        },
        "shares": {
            "shares_outstanding": big(i.get("sharesOutstanding")),
            "short_pct_of_float": pct(i.get("shortPercentOfFloat")),
            "short_ratio_days": num(i.get("shortRatio")),
            "insider_held_%": pct(i.get("heldPercentInsiders")),
            "institution_held_%": pct(i.get("heldPercentInstitutions")),
        },
        "analyst": {
            "recommendation": i.get("recommendationKey"),
            "num_analysts": i.get("numberOfAnalystOpinions"),
            "target_mean": num(i.get("targetMeanPrice")),
            "target_low": num(i.get("targetLowPrice")),
            "target_high": num(i.get("targetHighPrice")),
            "upside_to_mean_%": pct(i["targetMeanPrice"] / i["currentPrice"] - 1)
            if i.get("targetMeanPrice") and i.get("currentPrice") else None,
        },
        "income_annual": fmt_rows(ia),
        "income_quarterly": fmt_rows(iq),
        "cashflow_annual": fmt_rows(st["cashflow_annual"]),
        "quality_scores": quality_scores(ticker),
        "source": "yfinance",
    }
    return clean(out)


def _cagr(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return None
    return (vals[-1] / vals[0]) ** (1 / (len(vals) - 1)) - 1


def quality_scores(ticker: str) -> dict[str, Any]:
    """Piotroski F-Score（0-9）和 Altman Z-Score，用年报计算。"""
    st = statements(ticker)
    ia, ca, ba = st["income_annual"], st["cashflow_annual"], st["balance_annual"]
    out: dict[str, Any] = {}
    try:
        if len(ia) >= 2 and len(ba) >= 2 and len(ca) >= 1:
            i1, i0 = ia[-1], ia[-2]
            b1, b0 = ba[-1], ba[-2]
            c1 = ca[-1]

            def g(d, k):
                return d.get(k)

            checks = {}
            roa1 = g(i1, "net_income") / g(b1, "total_assets") if g(i1, "net_income") is not None and g(b1, "total_assets") else None
            roa0 = g(i0, "net_income") / g(b0, "total_assets") if g(i0, "net_income") is not None and g(b0, "total_assets") else None
            checks["正ROA"] = roa1 is not None and roa1 > 0
            checks["正经营现金流"] = (g(c1, "operating_cf") or 0) > 0
            checks["ROA提升"] = roa1 is not None and roa0 is not None and roa1 > roa0
            checks["经营现金流>净利润"] = g(c1, "operating_cf") is not None and g(i1, "net_income") is not None and g(c1, "operating_cf") > g(i1, "net_income")
            lev1 = (g(b1, "total_debt") or 0) / g(b1, "total_assets") if g(b1, "total_assets") else None
            lev0 = (g(b0, "total_debt") or 0) / g(b0, "total_assets") if g(b0, "total_assets") else None
            checks["杠杆下降"] = lev1 is not None and lev0 is not None and lev1 <= lev0
            cr1 = g(b1, "current_assets") / g(b1, "current_liabilities") if g(b1, "current_assets") and g(b1, "current_liabilities") else None
            cr0 = g(b0, "current_assets") / g(b0, "current_liabilities") if g(b0, "current_assets") and g(b0, "current_liabilities") else None
            checks["流动比率提升"] = cr1 is not None and cr0 is not None and cr1 > cr0
            checks["未增发股票"] = g(b1, "shares") is not None and g(b0, "shares") is not None and g(b1, "shares") <= g(b0, "shares") * 1.01
            gm1 = g(i1, "gross_profit") / g(i1, "revenue") if g(i1, "gross_profit") and g(i1, "revenue") else None
            gm0 = g(i0, "gross_profit") / g(i0, "revenue") if g(i0, "gross_profit") and g(i0, "revenue") else None
            checks["毛利率提升"] = gm1 is not None and gm0 is not None and gm1 > gm0
            at1 = g(i1, "revenue") / g(b1, "total_assets") if g(i1, "revenue") and g(b1, "total_assets") else None
            at0 = g(i0, "revenue") / g(b0, "total_assets") if g(i0, "revenue") and g(b0, "total_assets") else None
            checks["资产周转率提升"] = at1 is not None and at0 is not None and at1 > at0
            out["piotroski_f"] = sum(checks.values())
            out["piotroski_detail"] = {k: bool(v) for k, v in checks.items()}
    except Exception:
        pass
    try:
        i = info(ticker)
        b1, i1 = ba[-1], ia[-1]
        ta = b1.get("total_assets")
        wc = b1.get("working_capital") or ((b1.get("current_assets") or 0) - (b1.get("current_liabilities") or 0))
        if ta and i.get("marketCap") and b1.get("total_liabilities"):
            z = (1.2 * wc / ta + 1.4 * (b1.get("retained_earnings") or 0) / ta
                 + 3.3 * (i1.get("operating_income") or 0) / ta
                 + 0.6 * i["marketCap"] / b1["total_liabilities"] + 1.0 * (i1.get("revenue") or 0) / ta)
            out["altman_z"] = num(z, 3)
            out["altman_zone"] = "安全" if z > 2.99 else ("灰色" if z > 1.81 else "危险")
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 期权
@cached(ttl=900)
def options_summary(ticker: str) -> dict[str, Any]:
    t = _tk(ticker)
    exps = list(t.options or [])
    if not exps:
        return {"symbol": ticker, "note": "该标的没有期权数据"}
    price = _fast(ticker).get("last_price")
    today = datetime.now().date()
    out = {"symbol": ticker.upper(), "price": num(price), "expirations": []}
    picks = []
    for e in exps:
        dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if dte >= 1 and not picks:
            picks.append((e, dte))
        if dte >= 25:
            picks.append((e, dte))
            break
    for e, dte in picks:
        try:
            ch = t.option_chain(e)
        except Exception:
            continue
        calls, puts = ch.calls, ch.puts
        if calls.empty or puts.empty or not price:
            continue
        atm_c = calls.iloc[(calls["strike"] - price).abs().argsort()[:1]]
        atm_p = puts.iloc[(puts["strike"] - price).abs().argsort()[:1]]
        straddle = float(atm_c["lastPrice"].iloc[0] + atm_p["lastPrice"].iloc[0])
        strikes = sorted(set(calls["strike"]).union(puts["strike"]))
        coi = calls.groupby("strike")["openInterest"].sum().fillna(0)
        poi = puts.groupby("strike")["openInterest"].sum().fillna(0)
        ck, cv_ = coi.index.to_numpy(dtype=float), coi.to_numpy(dtype=float)
        pk, pv_ = poi.index.to_numpy(dtype=float), poi.to_numpy(dtype=float)
        # 最大痛点：使期权买方总内在价值最小的到期价格
        pain = {k: float((np.clip(k - ck, 0, None) * cv_).sum() + (np.clip(pk - k, 0, None) * pv_).sum())
                for k in strikes}
        iv = (atm_c["impliedVolatility"].iloc[0] + atm_p["impliedVolatility"].iloc[0]) / 2
        if coi.sum() + poi.sum() == 0 or iv < 0.05:
            out["expirations"].append({
                "expiry": e, "dte": dte, "implied_move_%": pct(straddle / price) if straddle > 0 else None,
                "note": "期权链数据不完整（多见于非交易时段：持仓量为0、IV异常），OI/IV/最大痛点不可用",
            })
            continue
        top_c = calls.nlargest(3, "openInterest")[["strike", "openInterest"]].values.tolist()
        top_p = puts.nlargest(3, "openInterest")[["strike", "openInterest"]].values.tolist()
        cv, pv = calls["volume"].fillna(0).sum(), puts["volume"].fillna(0).sum()
        out["expirations"].append({
            "expiry": e, "dte": dte,
            "atm_strike": num(atm_c["strike"].iloc[0]),
            "atm_iv_%": pct(iv),
            "implied_move_%": pct(straddle / price),
            "put_call_volume_ratio": num(pv / cv) if cv else None,
            "put_call_oi_ratio": num(poi.sum() / coi.sum()) if coi.sum() else None,
            "max_pain": num(min(pain, key=pain.get)) if pain else None,
            "top_call_oi_strikes": [[num(s), int(o)] for s, o in top_c],
            "top_put_oi_strikes": [[num(s), int(o)] for s, o in top_p],
        })
    out["source"] = "yfinance (期权价格可能有15分钟延迟)"
    return clean(out)


# ---------------------------------------------------------------- 新闻与事件
@cached(ttl=900)
def news(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    items = _tk(ticker).news or []
    out = []
    for it in items[:limit]:
        c = it.get("content", it)
        out.append(clean({
            "title": c.get("title"),
            "publisher": (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher"),
            "time": c.get("pubDate") or (time.strftime("%Y-%m-%d %H:%M", time.gmtime(c["providerPublishTime"])) if c.get("providerPublishTime") else None),
            "summary": (c.get("summary") or "")[:300] or None,
        }))
    return out


@cached(ttl=3600)
def events(ticker: str) -> dict[str, Any]:
    t = _tk(ticker)
    out: dict[str, Any] = {"symbol": ticker.upper()}
    try:
        cal = t.calendar or {}
        ed = cal.get("Earnings Date")
        out["next_earnings"] = [str(d) for d in ed] if isinstance(ed, list) else (str(ed) if ed else None)
        if out["next_earnings"]:
            first = out["next_earnings"][0] if isinstance(out["next_earnings"], list) else out["next_earnings"]
            out["days_to_earnings"] = (datetime.strptime(first[:10], "%Y-%m-%d").date() - datetime.now().date()).days
        out["eps_estimate"] = {k: cal.get(k) for k in ("Earnings Low", "Earnings Average", "Earnings High")}
        out["revenue_estimate_avg"] = big(cal.get("Revenue Average"))
        out["ex_dividend_date"] = str(cal["Ex-Dividend Date"]) if cal.get("Ex-Dividend Date") else None
    except Exception:
        pass
    try:
        eh = t.earnings_dates
        if eh is not None and not eh.empty:
            past = eh.dropna(subset=["Reported EPS"]).head(4)
            out["recent_earnings_surprises"] = [
                {"date": idx.strftime("%Y-%m-%d"), "eps_est": num(r.get("EPS Estimate")),
                 "eps_actual": num(r.get("Reported EPS")), "surprise_%": num(r.get("Surprise(%)"))}
                for idx, r in past.iterrows()
            ]
    except Exception:
        pass
    try:
        ud = t.upgrades_downgrades
        if ud is not None and not ud.empty:
            ud = ud.sort_index(ascending=False).head(8)
            out["analyst_actions"] = [
                {"date": idx.strftime("%Y-%m-%d"), "firm": r.get("Firm"), "action": r.get("Action"),
                 "from": r.get("FromGrade"), "to": r.get("ToGrade"),
                 "pt": num(r.get("currentPriceTarget")), "prior_pt": num(r.get("priorPriceTarget"))}
                for idx, r in ud.iterrows()
            ]
    except Exception:
        pass
    try:
        ins = t.insider_transactions
        if ins is not None and not ins.empty:
            ins = ins.copy()
            ins["Start Date"] = pd.to_datetime(ins["Start Date"])
            recent = ins[ins["Start Date"] > pd.Timestamp.now() - pd.Timedelta(days=180)]
            text = recent["Text"].fillna("").str.lower()
            buys = recent[text.str.contains("purchase")]
            sells = recent[text.str.contains("sale")]
            out["insider_6m"] = {
                "buy_count": len(buys), "buy_value": big(buys["Value"].fillna(0).sum()),
                "sell_count": len(sells), "sell_value": big(sells["Value"].fillna(0).sum()),
                "notable": [
                    {"date": r["Start Date"].strftime("%Y-%m-%d"), "who": r.get("Insider"), "position": r.get("Position"),
                     "text": (r.get("Text") or "")[:80], "value": big(r.get("Value"))}
                    for _, r in recent.nlargest(4, "Value").iterrows()
                ],
            }
    except Exception:
        pass
    try:
        ih = t.institutional_holders
        if ih is not None and not ih.empty:
            out["top_institutions"] = [
                {"holder": r.get("Holder"), "pct_held": pct(r.get("pctHeld")), "pct_change": pct(r.get("pctChange"))}
                for _, r in ih.head(6).iterrows()
            ]
    except Exception:
        pass
    return clean(out)


@cached(ttl=86400)
def peers(ticker: str) -> list[str]:
    """同行业可比公司：用 yfinance 的行业成分（取市值前几名）。"""
    i = info(ticker)
    industry_key = i.get("industryKey")
    if not industry_key:
        return []
    try:
        ind = yf.Industry(industry_key)
        top = ind.top_companies
        if top is None or top.empty:
            return []
        syms = [s for s in top.index.tolist() if s.upper() != ticker.upper()]
        return syms[:6]
    except Exception:
        return []


def peer_compare(ticker: str, others: list[str] | None = None) -> dict[str, Any]:
    syms = [ticker.upper()] + [s.upper() for s in (others or peers(ticker))][:6]
    rows = []
    for s in syms:
        i = info(s)
        if not i:
            continue
        rows.append({
            "symbol": s, "name": i.get("shortName"), "market_cap": big(i.get("marketCap")),
            "pe_ttm": num(i.get("trailingPE")), "pe_fwd": num(i.get("forwardPE")),
            "ps": num(i.get("priceToSalesTrailing12Months")), "ev_ebitda": num(i.get("enterpriseToEbitda")),
            "rev_growth_%": pct(i.get("revenueGrowth")), "gross_margin_%": pct(i.get("grossMargins")),
            "op_margin_%": pct(i.get("operatingMargins")), "roe_%": pct(i.get("returnOnEquity")),
        })
    return clean({"industry": info(ticker).get("industry"), "peers": rows})
