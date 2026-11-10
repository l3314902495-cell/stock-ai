"""SEC EDGAR XBRL（官方免费）：10 年以上年报数据，用于长线趋势和历史估值分位。

SEC 要求 User-Agent 带联系方式，请在 config.yaml 的 data.sec_user_agent 里填写自己的。
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import requests

from ..cache import cached
from ..config import data_proxy, get_config
from ..util import big, clean, num, pct

_TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}


def _get(url: str) -> Any:
    ua = get_config()["data"]["sec_user_agent"]
    proxy = data_proxy()
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30,
                     proxies={"https": proxy} if proxy else None)
    r.raise_for_status()
    return r.json()


@cached(ttl=7 * 86400)
def _cik_map() -> dict[str, int]:
    data = _get("https://www.sec.gov/files/company_tickers.json")
    return {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}


@cached(ttl=86400)
def _facts(cik: int) -> dict[str, Any]:
    return _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")


def _annual_series(facts: dict, tags: list[str]) -> dict[int, tuple[float, str, str]]:
    """返回 {财年: (数值, 申报日期, 期末日期)}。同一年取最新申报的值（后续年报会重述旧数据）。"""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    merged: dict[int, tuple[float, str, str]] = {}
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        best: dict[int, tuple[float, str, str]] = {}
        for unit, items in node.get("units", {}).items():
            for it in items:
                if it.get("form") not in ("10-K", "10-K/A", "20-F") or it.get("fp") != "FY":
                    continue
                if "start" in it:  # 只要完整年度区间
                    days = (pd.Timestamp(it["end"]) - pd.Timestamp(it["start"])).days
                    if days < 340 or days > 380:
                        continue
                year = pd.Timestamp(it["end"]).year
                filed = it.get("filed", "")
                if year not in best or filed > best[year][1]:
                    best[year] = (it["val"], filed, it["end"])
        # 多个 tag 合并：先出现的 tag 优先（公司换会计科目名时补齐年份）
        for y, v in best.items():
            merged.setdefault(y, v)
    return dict(sorted(merged.items()))


def _split_factor_after(splits: pd.Series, date_str: str) -> float:
    """date_str 之后发生的拆股倍数之积（用于把旧申报的 EPS 调整为当前股本口径）。"""
    if splits is None or splits.empty or not date_str:
        return 1.0
    d = pd.Timestamp(date_str)
    idx = splits.index.tz_localize(None) if splits.index.tz is not None else splits.index
    f = 1.0
    for when, ratio in zip(idx, splits.values):
        if when > d and ratio > 0:
            f *= float(ratio)
    return f


@cached(ttl=86400)
def long_history(ticker: str, years: int = 12) -> dict[str, Any]:
    cik = _cik_map().get(ticker.upper().replace("-", "."))
    if cik is None:
        cik = _cik_map().get(ticker.upper())
    if cik is None:
        return {"symbol": ticker, "note": "SEC 中没有该代码（可能是 ETF、外国公司 ADR 或非美股）"}
    facts = _facts(cik)
    series = {k: _annual_series(facts, tags) for k, tags in _TAGS.items()}
    try:
        import yfinance as yf

        splits = yf.Ticker(ticker).splits
    except Exception:
        splits = None
    all_years = sorted(set().union(*[s.keys() for s in series.values()]))[-years:]
    rows = []
    for y in all_years:
        r: dict[str, Any] = {"fy": y}
        for k, s in series.items():
            if y not in s:
                r[k] = None
                continue
            val, filed, end = s[y]
            r.setdefault("period_end", end)
            f = _split_factor_after(splits, filed)
            if k == "eps_diluted":
                val = val / f
            elif k == "shares_diluted":
                val = val * f
            r[k] = val
        if r.get("operating_cf") is not None and r.get("capex") is not None:
            r["fcf"] = r["operating_cf"] - r["capex"]
        rows.append(r)

    def cagr(key: str, n: int) -> float | None:
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        if len(vals) < n + 1 or vals[-1 - n] <= 0 or vals[-1] <= 0:
            return None
        return (vals[-1] / vals[-1 - n]) ** (1 / n) - 1

    # 历史 PE：每个财年末收盘价 / 当年稀释 EPS
    pe_hist = []
    try:
        from . import us_stock

        px = us_stock.history(ticker, "1wk", "max")["Close"]
        px.index = px.index.tz_localize(None) if px.index.tz is not None else px.index
        for r in rows:
            eps = r.get("eps_diluted")
            if not eps or eps <= 0:
                continue
            end = pd.Timestamp(r.get("period_end") or f"{r['fy']}-12-31")
            p = px[px.index <= end]
            if len(p):
                pe_hist.append({"fy": r["fy"], "pe": num(p.iloc[-1] / eps)})
    except Exception:
        pass

    out = {
        "symbol": ticker.upper(),
        "entity": facts.get("entityName"),
        "annual": [
            {k: (v if k == "period_end" else num(v) if k in ("fy", "eps_diluted") else big(v)) for k, v in r.items()}
            for r in rows
        ],
        "cagr": {
            "revenue_3y_%": pct(cagr("revenue", 3)), "revenue_5y_%": pct(cagr("revenue", 5)),
            "revenue_10y_%": pct(cagr("revenue", 10)),
            "eps_3y_%": pct(cagr("eps_diluted", 3)), "eps_5y_%": pct(cagr("eps_diluted", 5)),
            "fcf_5y_%": pct(cagr("fcf", 5)),
        },
        "historical_pe_by_fy": pe_hist,
        "note": "EPS 已按拆股调整；历史PE = 财年末价格 / 当财年稀释EPS，为近似值，仅作分位参考",
        "source": "SEC EDGAR XBRL",
    }
    return clean(out)
