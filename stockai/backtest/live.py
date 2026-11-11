"""把回测里的策略用到今天的真实行情上：算出每个标的当前应持有还是空仓。

- 参数：用滚动前推在开发期最后一年（2024）选出的参数，不读取验证集
- 行情：Yahoo 实时日线（与回测快照同源），未收盘的当天 K 线默认丢弃，只用已收盘数据
- 跨资产参考（如 BTC 过滤）同样使用实时数据
- 仓位含义与回测一致：今天收盘后的目标仓位，下一个交易日开盘执行
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .. import strategies
from ..analytics.technicals import last_bar_status
from . import data
from .evaluate import load_scoreboard, walk_forward


def live_daily(symbol: str, drop_incomplete: bool = True) -> pd.DataFrame:
    from ..data import us_stock

    df = us_stock.history(symbol, "1d", "5y").copy()
    asset = "crypto" if symbol.endswith("-USD") else "stock"
    complete = last_bar_status(df, "1d", asset)["complete"]
    idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")]
    df.attrs["live"] = True
    df.attrs["last_bar_complete"] = complete
    if drop_incomplete and not complete:
        df = df.iloc[:-1]
        df.attrs["dropped_incomplete_bar"] = True
    return df


def current_params(strategy: str, group: str) -> dict[str, Any]:
    st = strategies.get(strategy)
    if len(st.combos(group)) <= 1:
        return st.combos(group)[0]
    return walk_forward(strategy, group)["chosen_params"][-1]["params"]


def signals(strategy: str | None = None, group: str = "crypto") -> dict[str, Any]:
    if strategy is None:
        champ = (load_scoreboard().get("groups") or {}).get(group)
        if not champ:
            raise ValueError(f"{group} 组还没有冠军策略，请指定策略名")
        strategy = champ["strategy"]
    st = strategies.get(strategy)
    if group not in st.groups:
        raise ValueError(f"{strategy} 不适用于 {group}")
    params = current_params(strategy, group)
    rows = []
    for sym in data.GROUPS[group]:
        try:
            df = live_daily(sym)
            sig = st.signal(df, **params).astype(float)
            now, prev = float(sig.iloc[-1]), float(sig.iloc[-2])
            if now > prev:
                action = "买入" if prev == 0 else "加仓"
            elif now < prev:
                action = "卖出" if now == 0 else "减仓"
            else:
                action = "持有" if now > 0 else "空仓"
            rows.append({"symbol": sym, "date": str(df.index[-1].date()), "close": float(f"{float(df['Close'].iloc[-1]):.6g}"),
                         "position": round(now, 3), "prev_position": round(prev, 3), "action": action})
        except Exception as e:
            rows.append({"symbol": sym, "error": str(e)[:150]})
    weight = 1 / len(data.GROUPS[group])
    return {"strategy": strategy, "group": group, "params": params, "rows": rows,
            "portfolio_exposure": round(sum(r.get("position", 0) for r in rows) * weight, 3),
            "note": f"组内等权：每个标的占该组资金的 {weight:.1%}；仓位为已收盘数据给出的目标仓位，下一交易日开盘执行"}
