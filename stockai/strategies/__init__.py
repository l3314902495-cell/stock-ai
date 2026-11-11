"""可回测的规则策略。

每个策略是一个 Strategy：
- signal(df, **params) -> pd.Series，值在 [0, 1]，表示第 d 天收盘后的目标仓位
  （只能使用第 d 天及之前的数据——评测时会自动做"因果性检查"，偷看未来会被判无效）
- grid：参数候选（每组参数 ≤ 4 个、组合数 ≤ 16），滚动前推时在训练期内选择
- groups：适用的资产组（stocks / crypto）

新增策略：在本包里写一个模块，用 @register 注册即可。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

MAX_PARAMS = 4
MAX_COMBOS = 16


@dataclass
class Strategy:
    name: str
    signal: Callable[..., pd.Series]
    grid: dict[str, list[Any]] = field(default_factory=dict)
    groups: tuple[str, ...] = ("stocks", "crypto")
    description: str = ""
    group_grid: dict[str, dict[str, list[Any]]] = field(default_factory=dict)  # 按资产组覆盖参数候选

    def combos(self, group: str) -> list[dict[str, Any]]:
        g = self.group_grid.get(group, self.grid)
        if not g:
            return [{}]
        keys = list(g)
        return [dict(zip(keys, v)) for v in itertools.product(*(g[k] for k in keys))]

    def validate(self) -> None:
        for grp in self.groups:
            g = self.group_grid.get(grp, self.grid)
            if len(g) > MAX_PARAMS:
                raise ValueError(f"{self.name}: 参数个数 {len(g)} 超过上限 {MAX_PARAMS}")
            if len(self.combos(grp)) > MAX_COMBOS:
                raise ValueError(f"{self.name}: {grp} 参数组合 {len(self.combos(grp))} 超过上限 {MAX_COMBOS}")


REGISTRY: dict[str, Strategy] = {}


def register(name: str, grid: dict | None = None, groups: tuple[str, ...] = ("stocks", "crypto"),
             description: str = "", group_grid: dict | None = None):
    def deco(fn):
        s = Strategy(name, fn, grid or {}, groups, description, group_grid or {})
        s.validate()
        REGISTRY[name] = s
        return fn

    return deco


def reference(df: pd.DataFrame, symbol: str, field: str = "Close") -> pd.Series:
    """跨资产策略读取参考标的（如 BTC、SPY），自动对齐到 df 的日期。

    遵守验证集锁（与被评测资产使用同一数据范围），并且只向前填充，不会引入未来数据。
    """
    if df.attrs.get("live"):  # 实盘信号：参考标的也用实时日线
        from ..backtest.live import live_daily

        ref = live_daily(symbol)[field]
        return ref.reindex(ref.index.union(df.index)).ffill().reindex(df.index)
    from ..backtest import data

    ref = data.load(symbol, holdout=bool(df.attrs.get("holdout", False)))[field]
    return ref.reindex(ref.index.union(df.index)).ffill().reindex(df.index)


def get(name: str) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"未知策略 {name}，可选：{', '.join(REGISTRY)}")
    return REGISTRY[name]


from . import baseline, exp_trend  # noqa: E402,F401  注册内置策略与实验策略
