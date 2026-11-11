"""技术指标——全部用代码计算，AI 只负责解读。

输入统一为 OHLCV DataFrame（列：Open High Low Close Volume，DatetimeIndex）。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..util import clean, num, pct


# ---------------------------------------------------------------- 基础指标
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # 区间内没有下跌：RSI = 100；完全没有波动：50
    out = out.mask((dn == 0) & (up > 0), 100.0).mask((dn == 0) & (up == 0), 50.0)
    return out.where(d.notna().cumsum() > 0)


def macd(s: pd.Series, f: int = 12, sl: int = 26, sig: int = 9):
    line = ema(s, f) - ema(s, sl)
    signal = ema(line, sig)
    return line, signal, line - signal


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["High"].diff()
    dn = -df["Low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, 1)
    atr_n = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0):
    m = sma(s, n)
    sd = s.rolling(n).std()
    return m + k * sd, m, m - k * sd


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["Close"].diff()).fillna(0)
    return (sign * df["Volume"]).cumsum()


# ---------------------------------------------------------------- 结构
def swing_levels(df: pd.DataFrame, window: int = 5, tol: float = 0.015, max_levels: int = 4) -> dict[str, list]:
    """用摆动高低点聚类得到支撑/压力位，返回 [价位, 触及次数]。"""
    h, l = df["High"].values, df["Low"].values
    pts = []
    for i in range(window, len(df) - window):
        if h[i] == h[i - window:i + window + 1].max():
            pts.append(h[i])
        if l[i] == l[i - window:i + window + 1].min():
            pts.append(l[i])
    pts.sort()
    clusters: list[list[float]] = []
    for p in pts:
        if clusters and abs(p / np.mean(clusters[-1]) - 1) < tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    price = df["Close"].iloc[-1]
    levels = [(float(np.mean(c)), len(c)) for c in clusters]
    sup = sorted([lv for lv in levels if lv[0] < price], key=lambda x: -x[0])[:max_levels]
    res = sorted([lv for lv in levels if lv[0] > price], key=lambda x: x[0])[:max_levels]
    return {
        "support": [[num(p), n] for p, n in sup],
        "resistance": [[num(p), n] for p, n in res],
    }


def volume_profile(df: pd.DataFrame, bins: int = 30) -> dict[str, Any]:
    """成交量分布：POC（成交最密集价位）和 70% 价值区间。"""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    lo, hi = df["Low"].min(), df["High"].max()
    if hi <= lo:
        return {}
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(typical, edges) - 1, 0, bins - 1)
    vol = np.bincount(idx, weights=df["Volume"].fillna(0).values, minlength=bins)
    if vol.sum() == 0:
        return {}
    poc = int(vol.argmax())
    order = np.argsort(vol)[::-1]
    acc, area = 0.0, []
    for i in order:
        area.append(i)
        acc += vol[i]
        if acc >= 0.7 * vol.sum():
            break
    mid = (edges[:-1] + edges[1:]) / 2
    return {"poc": num(mid[poc]), "value_area": [num(edges[min(area)]), num(edges[max(area) + 1])]}


def _cross_info(fast: pd.Series, slow: pd.Series, name: str) -> str | None:
    diff = (fast - slow).dropna()
    if len(diff) < 2:
        return None
    sign = np.sign(diff)
    changes = np.where(sign.values[1:] != sign.values[:-1])[0]
    if not len(changes):
        return f"{name}: 区间内无交叉，当前{'在上' if sign.iloc[-1] > 0 else '在下'}"
    last = changes[-1] + 1
    bars_ago = len(diff) - 1 - last
    kind = "金叉(上穿)" if sign.iloc[last] > 0 else "死叉(下穿)"
    return f"{name}: {bars_ago} 根K线前出现{kind}"


# ---------------------------------------------------------------- K 线是否收盘
_DUR = {"15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30), "1h": pd.Timedelta(hours=1),
        "4h": pd.Timedelta(hours=4), "1d": pd.Timedelta(days=1), "1wk": pd.Timedelta(days=7)}


def last_bar_status(df: pd.DataFrame, interval: str, asset_type: str = "crypto",
                    now: pd.Timestamp | None = None) -> dict[str, Any]:
    """判断最后一根 K 线是否已经走完。美股按美东 16:00 收盘计算，加密按 UTC 自然周期。"""
    start = df.index[-1]
    if start.tz is None:
        start = start.tz_localize("UTC")
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    dur = _DUR.get(interval, pd.Timedelta(days=1))
    if asset_type == "stock":
        st = start.tz_convert("America/New_York")
        close = st.normalize() + pd.Timedelta(hours=16)
        open_ = st.normalize() + pd.Timedelta(hours=9, minutes=30)
        if interval == "1d":
            end, start = close, max(st, open_)
        elif interval == "1wk":
            end, start = st.normalize() + pd.Timedelta(days=4 - st.weekday(), hours=16), max(st, open_)
        else:
            end, start = min(st + dur, close), st
    else:
        end = start + dur
    complete = bool(now >= end)
    progress = 1.0 if complete else max(0.0, min(1.0, (now - start) / (end - start)))
    return {"complete": complete, "progress": progress}


# ---------------------------------------------------------------- 汇总
def summarize(df: pd.DataFrame, interval: str, recent_bars: int = 8, asset_type: str = "crypto") -> dict[str, Any]:
    """单周期技术面摘要。"""
    c = df["Close"]
    if len(c) < 30:
        return {"interval": interval, "note": f"数据太少（{len(c)} 根）"}
    price = float(c.iloc[-1])
    ma = {n: sma(c, n) for n in (20, 50, 200) if len(c) >= n}
    e21 = ema(c, 21)
    r = rsi(c)
    m_line, m_sig, m_hist = macd(c)
    a = atr(df)
    adx_v, pdi, mdi = adx(df)
    bu, bm, bl = bollinger(c)
    vol20 = df["Volume"].rolling(20).mean()

    # 均线排列
    trend = []
    vals = {n: float(s.iloc[-1]) for n, s in ma.items() if not np.isnan(s.iloc[-1])}
    if vals:
        above = [f"MA{n}" for n, v in vals.items() if price > v]
        below = [f"MA{n}" for n, v in vals.items() if price <= v]
        trend.append(f"价格在 {','.join(above) or '无'} 之上；在 {','.join(below) or '无'} 之下")
        if all(k in vals for k in (20, 50, 200)):
            if vals[20] > vals[50] > vals[200]:
                trend.append("均线多头排列 (MA20>MA50>MA200)")
            elif vals[20] < vals[50] < vals[200]:
                trend.append("均线空头排列 (MA20<MA50<MA200)")
            else:
                trend.append("均线交织，无明确排列")
    if 50 in ma and 200 in ma:
        ci = _cross_info(ma[50], ma[200], "MA50/MA200")
        if ci:
            trend.append(ci)
    ci = _cross_info(m_line, m_sig, "MACD/信号线")
    if ci:
        trend.append(ci)

    slope20 = None
    if 20 in ma and len(ma[20].dropna()) > 5:
        slope20 = ma[20].iloc[-1] / ma[20].iloc[-6] - 1

    obv_s = obv(df)
    obv_trend = None
    if len(obv_s) > 20:
        y = obv_s.iloc[-20:].values
        obv_trend = "上升" if np.polyfit(range(20), y, 1)[0] > 0 else "下降"

    lookback = df.iloc[-min(len(df), 250):]
    out = {
        "interval": interval,
        "bars": len(df),
        "last_bar_time": df.index[-1],
        "price": num(price),
        "ma": {f"ma{n}": num(v) for n, v in vals.items()},
        "ema21": num(e21.iloc[-1]),
        "ma20_slope_5bars_%": pct(slope20),
        "rsi14": num(r.iloc[-1], 3),
        "rsi14_5bars_ago": num(r.iloc[-6], 3) if len(r) > 6 else None,
        "macd": {"line": num(m_line.iloc[-1]), "signal": num(m_sig.iloc[-1]), "hist": num(m_hist.iloc[-1]),
                 "hist_prev": num(m_hist.iloc[-2])},
        "adx14": num(adx_v.iloc[-1], 3),
        "di_plus": num(pdi.iloc[-1], 3),
        "di_minus": num(mdi.iloc[-1], 3),
        "trend_strength": "强趋势" if adx_v.iloc[-1] > 25 else ("弱趋势/震荡" if adx_v.iloc[-1] < 20 else "趋势形成中"),
        "atr14": num(a.iloc[-1]),
        "atr14_%": pct(a.iloc[-1] / price),
        "bollinger": {"upper": num(bu.iloc[-1]), "mid": num(bm.iloc[-1]), "lower": num(bl.iloc[-1]),
                      "%b": num((price - bl.iloc[-1]) / (bu.iloc[-1] - bl.iloc[-1])) if bu.iloc[-1] != bl.iloc[-1] else None,
                      "bandwidth_%": pct((bu.iloc[-1] - bl.iloc[-1]) / bm.iloc[-1]),
                      "bandwidth_pctile_1y": num((((bu - bl) / bm).iloc[-250:] <= ((bu - bl) / bm).iloc[-1]).mean())},
        "volume_vs_20avg": num(df["Volume"].iloc[-1] / vol20.iloc[-1]) if vol20.iloc[-1] else None,
        "obv_trend_20bars": obv_trend,
        "signals": trend,
        "levels": swing_levels(lookback),
        "volume_profile": volume_profile(lookback),
        "range_high": num(lookback["High"].max()),
        "range_low": num(lookback["Low"].min()),
        "last_bar_complete": True,
        "recent_bars": [
            {"t": idx, "o": num(r_["Open"]), "h": num(r_["High"]), "l": num(r_["Low"]), "c": num(r_["Close"]),
             "v": num(r_["Volume"], 3)}
            for idx, r_ in df.iloc[-recent_bars:].iterrows()
        ],
    }
    if df.attrs.get("bad_ticks_fixed"):
        out["data_note"] = f"已用日线高低点修正 {df.attrs['bad_ticks_fixed']} 根异常日内K线"
    status = last_bar_status(df, interval, asset_type)
    if not status["complete"]:
        p = status["progress"]
        out["last_bar_complete"] = False
        out["last_bar_progress_%"] = round(p * 100, 1)
        out["rsi14_last_closed_bar"] = num(r.iloc[-2], 3)
        if vol20.iloc[-2] and p > 0.05:
            out["volume_vs_20avg_projected"] = num(df["Volume"].iloc[-1] / p / vol20.iloc[-2])
        out["note"] = "最后一根K线尚未收盘：价格/指标含盘中数据，量比请看 projected 预估值"
    return clean(out)


def performance(daily: pd.DataFrame, bench: pd.DataFrame | None = None, periods_per_year: int = 252) -> dict[str, Any]:
    """收益/风险统计（基于日线）。股票一年 252 个交易日，加密 365。"""
    c = daily["Close"]
    price = float(c.iloc[-1])
    idx = c.index

    def ret_days(n):
        return pct(price / c.iloc[-n - 1] - 1) if len(c) > n else None

    ytd = None
    try:
        prev = c[idx.year < idx[-1].year]
        if len(prev):
            ytd = pct(price / prev.iloc[-1] - 1)
    except Exception:
        pass
    ann = periods_per_year
    m = ann / 252
    last1y = c.iloc[-ann:] if len(c) > ann else c
    dd = (last1y / last1y.cummax() - 1).min()
    rets = c.pct_change().dropna()
    vol = rets.iloc[-ann:].std() * np.sqrt(ann) if len(rets) > 20 else None
    out = {
        "return_%": {"1d": ret_days(1), "5d": ret_days(round(5 * m)), "1m": ret_days(round(21 * m)),
                     "3m": ret_days(round(63 * m)), "6m": ret_days(round(126 * m)), "1y": ret_days(ann), "ytd": ytd},
        "max_drawdown_1y_%": pct(dd),
        "annual_volatility_%": pct(vol) if vol is not None else None,
        "from_1y_high_%": pct(price / last1y.max() - 1),
        "from_1y_low_%": pct(price / last1y.min() - 1),
    }
    if bench is not None and len(bench) > 70:
        b = bench["Close"]
        b.index = b.index.tz_localize(None) if b.index.tz is not None else b.index
        cc = c.copy()
        cc.index = cc.index.tz_localize(None) if cc.index.tz is not None else cc.index
        cc.index = cc.index.normalize()
        b.index = b.index.normalize()
        j = pd.concat([cc, b], axis=1, join="inner").dropna()
        j.columns = ["a", "b"]
        if len(j) > 70:
            rs = {}
            for label, n in (("1m", round(21 * m)), ("3m", round(63 * m)), ("6m", round(126 * m))):
                if len(j) > n:
                    rs[label] = pct((j["a"].iloc[-1] / j["a"].iloc[-n - 1]) - (j["b"].iloc[-1] / j["b"].iloc[-n - 1]))
            out["excess_return_vs_benchmark_%"] = rs
            ra, rb = j["a"].pct_change().dropna(), j["b"].pct_change().dropna()
            if len(ra) > 60:
                cov = np.cov(ra.iloc[-250:], rb.iloc[-250:])
                out["beta_1y"] = num(cov[0, 1] / cov[1, 1]) if cov[1, 1] else None
                out["correlation_1y"] = num(np.corrcoef(ra.iloc[-250:], rb.iloc[-250:])[0, 1])
    return clean(out)


def mtf_alignment(summaries: list[dict[str, Any]]) -> str:
    """多周期一致性的简单判断。"""
    parts = []
    for s in summaries:
        if "ma" not in s or "price" not in s:
            continue
        p = s["price"]
        ref = s["ma"].get("ma50") or s["ma"].get("ma20")
        e = s.get("ema21")
        if ref is None or e is None:
            continue
        state = "多" if p > ref and p > e else ("空" if p < ref and p < e else "中性")
        parts.append(f"{s['interval']}:{state}")
    if not parts:
        return "无法判断"
    states = {p.split(":")[1] for p in parts}
    verdict = "多周期共振" if len(states) == 1 and "中性" not in states else "多周期分歧"
    return f"{verdict}（{'，'.join(parts)}）"
