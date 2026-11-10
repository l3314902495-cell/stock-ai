"""自选股扫描：纯代码打分，不花 AI 的钱。排名靠前的再交给 AI 深入分析。

信号（日线为主）：
  趋势排列、突破/跌破 20 日高低点、接近一年新高、RSI 超买超卖、放量、布林收口、
  MACD 新交叉、接近支撑/压力（1 倍 ATR 内）、7 天内财报（美股）、资金费率/持仓异常（加密）
关注度 = 信号权重绝对值之和；方向分 = 带符号之和（正偏多、负偏空）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import symbols
from .analytics import technicals as ta
from .tools import ohlcv
from .util import num, pct


def _signals(sym: dict[str, Any]) -> dict[str, Any]:
    df = ohlcv(sym, "1d")
    if len(df) < 60:
        return {"symbol": sym["symbol"], "error": "数据太少"}
    s = ta.summarize(df, "1d", asset_type=sym["asset_type"])
    c, h, l = df["Close"], df["High"], df["Low"]
    price = float(c.iloc[-1])
    atr = s.get("atr14") or 0
    sig: list[tuple[str, int]] = []

    ma = s.get("ma", {})
    if all(k in ma for k in ("ma50", "ma200")):
        if price > ma["ma50"] > ma["ma200"]:
            sig.append(("多头排列", 1))
        elif price < ma["ma50"] < ma["ma200"]:
            sig.append(("空头排列", -1))
    hi20, lo20 = float(h.iloc[-21:-1].max()), float(l.iloc[-21:-1].min())
    if price > hi20:
        sig.append(("突破20日高点", 2))
    elif price < lo20:
        sig.append(("跌破20日低点", -2))
    hi1y = float(h.iloc[-252:].max())
    if price >= hi1y * 0.98:
        sig.append(("接近一年新高", 1))
    rsi = s.get("rsi14")
    if rsi is not None:
        if rsi < 30:
            sig.append((f"RSI超卖({rsi:.0f})", 1))
        elif rsi > 70:
            sig.append((f"RSI超买({rsi:.0f})", -1))
    vr = s.get("volume_vs_20avg_projected") or s.get("volume_vs_20avg")
    if vr and vr >= 2:
        up = c.iloc[-1] >= c.iloc[-2]
        sig.append((f"放量{vr:.1f}倍{'上涨' if up else '下跌'}", 1 if up else -1))
    bw = (s.get("bollinger") or {}).get("bandwidth_pctile_1y")
    if bw is not None and bw <= 0.1:
        sig.append(("布林收口(波动率低位，可能变盘)", 0))
    for txt in s.get("signals", []):
        if txt.startswith("MACD") and ("0 根" in txt or "1 根" in txt or "2 根" in txt):
            sig.append(("MACD新金叉" if "金叉" in txt else "MACD新死叉", 1 if "金叉" in txt else -1))
    lv = s.get("levels", {})
    if atr:
        for p, n in lv.get("support", [])[:1]:
            if price - p <= atr:
                sig.append((f"接近支撑{p}(触及{n}次)", 1))
        for p, n in lv.get("resistance", [])[:1]:
            if p - price <= atr:
                sig.append((f"接近压力{p}(触及{n}次)", -1))

    extra: dict[str, Any] = {}
    if sym["asset_type"] == "stock":
        try:
            from .data import us_stock

            d = us_stock.events(sym["symbol"]).get("days_to_earnings")
            if d is not None and 0 <= d <= 7:
                sig.append((f"{d}天后财报", 0))
                extra["days_to_earnings"] = d
        except Exception:
            pass
    else:
        try:
            from .data import crypto

            dv = crypto.derivatives(sym["symbol"])
            fr = dv.get("funding_rate_%")
            if fr is not None and abs(fr) >= 0.03:
                sig.append((f"资金费率极端({fr}%)", -1 if fr > 0 else 1))
            oi = dv.get("oi_change_24h_%")
            if oi is not None and abs(oi) >= 10:
                sig.append((f"持仓量24h{'+' if oi > 0 else ''}{oi}%", 0))
        except Exception:
            pass

    chg1d = pct(c.iloc[-1] / c.iloc[-2] - 1)
    return {
        "symbol": sym["symbol"],
        "asset_type": sym["asset_type"],
        "price": num(price),
        "chg_1d_%": chg1d,
        "chg_1m_%": pct(c.iloc[-1] / c.iloc[-22] - 1) if len(c) > 22 else None,
        "rsi14": rsi,
        "atr_%": s.get("atr14_%"),
        "attention": sum(abs(w) for _, w in sig) + sum(1 for _, w in sig if w == 0),
        "direction": sum(w for _, w in sig),
        "signals": [t for t, _ in sig],
        **extra,
    }


def scan(queries: list[str], workers: int = 4) -> list[dict[str, Any]]:
    def one(q: str):
        try:
            return _signals(symbols.resolve(q))
        except Exception as e:
            return {"symbol": q, "error": str(e)[:120]}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, queries))
    ok = sorted([r for r in rows if "error" not in r], key=lambda r: (-r["attention"], -abs(r["direction"])))
    return ok + [r for r in rows if "error" in r]
