"""基线策略：经典、简单、有公开研究依据的规则。后续优化以它们为起点和对照。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..analytics.technicals import atr, rsi, sma
from . import register


@register("buy_hold", description="买入持有（基准）")
def buy_hold(df: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=df.index)


@register("sma_trend", grid={"n": [50, 100, 150, 200]},
          description="收盘价在 N 日均线之上持有，否则空仓（经典趋势过滤）")
def sma_trend(df: pd.DataFrame, n: int) -> pd.Series:
    c = df["Close"]
    return (c > sma(c, n)).astype(float).where(sma(c, n).notna(), 0.0)


@register("sma_trend_voltarget",
          group_grid={"stocks": {"n": [100, 200], "target_vol": [0.10, 0.15, 0.20]},
                      "crypto": {"n": [100, 200], "target_vol": [0.30, 0.45, 0.60]}},
          description="均线趋势 + 波动率目标：仓位 = min(1, 目标波动率 / 近 20 日实现波动率)")
def sma_trend_voltarget(df: pd.DataFrame, n: int, target_vol: float) -> pd.Series:
    c = df["Close"]
    ppy = 365 if _is_7day(df) else 252
    rv = c.pct_change().rolling(20).std() * np.sqrt(ppy)
    size = (target_vol / rv).clip(upper=1.0)
    trend = (c > sma(c, n)).astype(float)
    return (trend * size).where(sma(c, n).notna() & rv.notna(), 0.0)


@register("donchian_atr", grid={"n": [20, 55], "k": [2.0, 3.0, 4.0]},
          description="突破 N 日最高收盘价入场；收盘跌破 (持仓期最高收盘 - k×ATR) 离场（吊灯止损）")
def donchian_atr(df: pd.DataFrame, n: int, k: float) -> pd.Series:
    c = df["Close"].values
    hh = df["Close"].rolling(n).max().shift(1).values  # 之前 N 日最高收盘（不含当天）
    a = atr(df, 14).values
    pos = np.zeros(len(c))
    inpos, peak = False, 0.0
    for i in range(len(c)):
        if np.isnan(hh[i]) or np.isnan(a[i]):
            continue
        if not inpos and c[i] > hh[i]:
            inpos, peak = True, c[i]
        elif inpos:
            peak = max(peak, c[i])
            if c[i] < peak - k * a[i]:
                inpos = False
        pos[i] = 1.0 if inpos else 0.0
    return pd.Series(pos, index=df.index)


@register("rsi2_pullback", groups=("stocks",), grid={"entry": [5, 10, 15], "trend_n": [150, 200]},
          description="上升趋势（收盘 > 均线）中 RSI(2) 低于阈值买入，收盘站上 5 日均线卖出")
def rsi2_pullback(df: pd.DataFrame, entry: float, trend_n: int) -> pd.Series:
    c = df["Close"]
    r2 = rsi(c, 2).values
    up = (c > sma(c, trend_n)).values
    exit_ = (c > sma(c, 5)).values
    pos = np.zeros(len(c))
    inpos = False
    for i in range(len(c)):
        if not inpos and up[i] and r2[i] < entry:
            inpos = True
        elif inpos and exit_[i]:
            inpos = False
        pos[i] = 1.0 if inpos else 0.0
    return pd.Series(pos, index=df.index)


def _is_7day(df: pd.DataFrame) -> bool:
    return bool((df.index[-60:].dayofweek >= 5).any())
