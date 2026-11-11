"""优化实验：加密组（只新增，不修改基线）。"""
from __future__ import annotations

import pandas as pd

from ..analytics.technicals import sma
from . import reference, register
from .baseline import donchian_atr, sma_trend


def _btc_regime(df: pd.DataFrame, n: int = 200) -> pd.Series:
    """BTC 收盘价在其 n 日均线之上 → 1，否则 0（BTC 自身也适用）。"""
    btc = reference(df, "BTC-USD")
    m = btc.rolling(n).mean()
    return (btc > m).astype(float).where(m.notna(), 0.0)


@register("sma_trend_btcfilter", groups=("crypto",), grid={"n": [100, 150, 200]},
          description="BTC 大盘过滤 + 自身均线趋势：BTC 在 200 日均线上方且自身收盘在 n 日均线上方才持有")
def sma_trend_btcfilter(df: pd.DataFrame, n: int) -> pd.Series:
    return sma_trend(df, n) * _btc_regime(df)


@register("donchian_btcfilter", groups=("crypto",), grid={"n": [20, 55], "k": [2.0, 3.0, 4.0]},
          description="BTC 大盘过滤 + 通道突破/吊灯止损：BTC 在 200 日均线上方时才允许持有")
def donchian_btcfilter(df: pd.DataFrame, n: int, k: float) -> pd.Series:
    return donchian_atr(df, n, k) * _btc_regime(df)
