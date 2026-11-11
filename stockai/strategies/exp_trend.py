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
