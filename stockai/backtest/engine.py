"""日线回测引擎（只做多 / 空仓，不加杠杆）。

时间约定（避免偷看未来）：
- 策略在第 d 天收盘后、只用 d 及之前的数据算出目标仓位 s[d] ∈ [0, 1]
- s[d] 在第 d+1 天开盘成交
- 第 d 天的收益 = 隔夜段（昨收→今开）按旧仓位 s[d-2] + 日内段（今开→今收）按新仓位 s[d-1]
- 交易成本在开盘成交时按 |s[d-1] - s[d-2]| × 单边成本 扣除
- 止损等条件只在收盘价上判断（不模拟盘中触发），偏保守、可复现
- 未投入的资金可按现金利率计息（cash 参数，日收益率）
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def strategy_returns(df: pd.DataFrame, signal: pd.Series, cost: float,
                     cash: pd.Series | None = None) -> pd.DataFrame:
    sig = signal.reindex(df.index).astype(float).clip(0.0, 1.0).fillna(0.0)
    held_intraday = sig.shift(1).fillna(0.0)   # 昨天收盘决定、今天开盘成交的仓位
    held_overnight = sig.shift(2).fillna(0.0)  # 昨天全天持有的仓位
    gap = (df["Open"] / df["Close"].shift(1) - 1).fillna(0.0)
    intra = (df["Close"] / df["Open"] - 1).fillna(0.0)
    turnover = (held_intraday - held_overnight).abs()
    ret = (1 + held_overnight * gap) * (1 + held_intraday * intra) - 1 - turnover * cost
    if cash is not None:  # 未投入的资金按现金利率计息
        ret = ret + (1 - held_intraday) * cash.reindex(df.index).fillna(0.0)
    return pd.DataFrame({"ret": ret, "pos": held_intraday, "turnover": turnover})


def trades(pos: pd.Series, ret: pd.Series) -> list[dict]:
    """把持仓切分成一笔笔交易（从 0 变为 >0 开始，回到 0 结束）。"""
    out, start, eq = [], None, 1.0
    p, r = pos.values, ret.values
    for i in range(len(p)):
        if start is None and p[i] > 0:
            start, eq = i, 1.0
        if start is not None:
            eq *= 1 + r[i]
            if p[i] == 0 or i == len(p) - 1:
                out.append({"entry": pos.index[start], "exit": pos.index[i], "bars": i - start + 1, "ret": eq - 1})
                start = None
    return out


def metrics(ret: pd.Series, periods_per_year: int, pos: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    if len(ret) < 2:
        return {}
    eq = (1 + ret).cumprod()
    years = len(ret) / periods_per_year
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    vol = ret.std() * np.sqrt(periods_per_year)
    dd = (eq / eq.cummax() - 1).min()
    out = {
        "cagr": float(cagr),
        "vol": float(vol),
        "sharpe": float(ret.mean() / ret.std() * np.sqrt(periods_per_year)) if ret.std() > 0 else 0.0,
        "max_dd": float(dd),
        "calmar": float(cagr / abs(dd)) if dd < 0 else 0.0,
        "total_return": float(eq.iloc[-1] - 1),
        "years": round(years, 2),
    }
    if pos is not None:
        tr = trades(pos.reindex(ret.index).fillna(0), ret)
        out["exposure"] = float((pos.reindex(ret.index) > 0).mean())
        out["trades"] = len(tr)
        out["win_rate"] = float(np.mean([t["ret"] > 0 for t in tr])) if tr else None
        out["avg_trade"] = float(np.mean([t["ret"] for t in tr])) if tr else None
    return out
