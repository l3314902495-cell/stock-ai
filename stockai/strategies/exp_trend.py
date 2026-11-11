"""优化实验：趋势类策略的改进版本（只新增，不修改基线）。"""
from __future__ import annotations

import pandas as pd

from ..analytics.technicals import sma
from . import register


@register("sma200_fixed", groups=("stocks",),
          description="实验#2：固定 200 日均线（不做参数选择），收盘在均线上方持有，否则空仓")
def sma200_fixed(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    m = sma(c, 200)
    return (c > m).astype(float).where(m.notna(), 0.0)


def _sma_band(df: pd.DataFrame, n: int, band: float) -> pd.Series:
    """均线缓冲带（滞回）：空仓时收盘 > 均线×(1+band) 才买入；持仓时收盘 < 均线×(1−band) 才卖出。"""
    c = df["Close"].values
    m = sma(df["Close"], n).values
    pos = [0.0] * len(c)
    inpos = False
    for i in range(len(c)):
        if m[i] != m[i]:  # NaN：均线尚未形成
            continue
        if not inpos and c[i] > m[i] * (1 + band):
            inpos = True
        elif inpos and c[i] < m[i] * (1 - band):
            inpos = False
        pos[i] = 1.0 if inpos else 0.0
    return pd.Series(pos, index=df.index)


@register("sma200_band1", groups=("stocks",),
          description="实验#3：固定 200 日均线 + 1% 缓冲带（站上均线×1.01 买入，跌破均线×0.99 卖出）")
def sma200_band1(df: pd.DataFrame) -> pd.Series:
    return _sma_band(df, 200, 0.01)


@register("sma200_band2", groups=("stocks",),
          description="实验#3：固定 200 日均线 + 2% 缓冲带（站上均线×1.02 买入，跌破均线×0.98 卖出）")
def sma200_band2(df: pd.DataFrame) -> pd.Series:
    return _sma_band(df, 200, 0.02)


@register("sma200_band3", groups=("stocks",),
          description="实验#3：固定 200 日均线 + 3% 缓冲带（站上均线×1.03 买入，跌破均线×0.97 卖出）")
def sma200_band3(df: pd.DataFrame) -> pd.Series:
    return _sma_band(df, 200, 0.03)
