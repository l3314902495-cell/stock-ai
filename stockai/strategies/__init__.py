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


def get(name: str) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"未知策略 {name}，可选：{', '.join(REGISTRY)}")
    return REGISTRY[name]


from . import baseline  # noqa: E402,F401  注册内置策略
