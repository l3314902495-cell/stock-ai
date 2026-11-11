"""策略评测：滚动前推（walk-forward）+ 组合评分 + 护栏。这是持续优化的"打分器"，规则见 GOALS.md。

流程（每个资产组独立）：
1. 每个参数组合、每个资产，在全部开发期数据上算出信号和每日收益（信号是因果的，所以可以一次算完）
2. 对每个测试年 Y：用 Y 之前 train_years 年的"组合收益"（组内资产等权）夏普最高的参数，作为 Y 年的参数
3. 把各测试年拼接成样本外收益，计算指标；基准 = 同一资产组等权买入持有
4. 护栏：交易笔数、最大回撤、因果性检查
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .. import strategies
from . import data
from .engine import metrics, strategy_returns, trades

SCOREBOARD = data.ROOT / "optimize" / "scoreboard.json"
WARMUP_BARS = 250          # 资产上市/有数据后的前 250 根 K 线不计入组合（指标预热期）
MIN_TRADES = 100           # 护栏：样本外交易笔数下限（组内合计）
MAX_DD_VS_BENCH = 0.60     # 护栏：样本外最大回撤 ≤ 基准最大回撤的 60%
MIN_CAGR_VS_BENCH = 0.60   # 护栏：样本外年化收益 ≥ 基准年化的 60%（防止"很安全但不赚钱"）
ACCEPT_MARGIN = 0.05       # 新策略主指标需超过现任冠军 5% 才算改进（过滤噪音）


@lru_cache(maxsize=4096)
def _asset_run(strategy: str, symbol: str, params_key: str, holdout: bool) -> pd.DataFrame:
    st = strategies.get(strategy)
    df = data.load(symbol, holdout=holdout)
    df.attrs["holdout"] = holdout  # 供 strategies.reference() 使用同一数据范围
    params = json.loads(params_key)
    sig = st.signal(df, **params)
    grp = data.group_of(symbol)
    out = strategy_returns(df, sig, data.COST[grp], data.cash_daily(grp, df.index, holdout))
    out.loc[out.index[:WARMUP_BARS], "ret"] = np.nan  # 预热期不计入
    return out


def _composite(strategy: str, group: str, params: dict, holdout: bool) -> tuple[pd.Series, dict[str, pd.DataFrame]]:
    key = json.dumps(params, sort_keys=True)
    runs = {s: _asset_run(strategy, s, key, holdout) for s in data.GROUPS[group]}
    rets = pd.concat({s: r["ret"] for s, r in runs.items()}, axis=1)
    return rets.mean(axis=1, skipna=True).dropna(), runs


def _sharpe(r: pd.Series) -> float:
    r = r.dropna()
    return float(r.mean() / r.std()) if len(r) > 20 and r.std() > 0 else -np.inf


def causality_check(strategy: str, group: str) -> dict[str, Any]:
    """信号只能用过去的数据：截断数据后算出的信号必须等于完整数据信号的前缀。"""
    st = strategies.get(strategy)
    df = data.load(data.GROUPS[group][0])
    combos = st.combos(group)
    params = combos[len(combos) // 2]
    full = st.signal(df, **params).astype(float)
    for frac in (0.35, 0.6, 0.85):
        cut = int(len(df) * frac)
        part = st.signal(df.iloc[:cut], **params).astype(float)
        a, b = full.iloc[:cut].fillna(-1).values, part.fillna(-1).values
        if not np.allclose(a, b, atol=1e-9):
            bad = int(np.argmax(~np.isclose(a, b, atol=1e-9)))
            return {"ok": False, "detail": f"截断到 {df.index[cut - 1]:%Y-%m-%d} 后，{df.index[bad]:%Y-%m-%d} 的信号发生变化（使用了未来数据）"}
    return {"ok": True}


def walk_forward(strategy: str, group: str, holdout: bool = False) -> dict[str, Any]:
    st = strategies.get(strategy)
    if group not in st.groups:
        raise ValueError(f"{strategy} 不适用于 {group}")
    cfg = data.WALK[group]
    combos = st.combos(group)
    comps = {json.dumps(c, sort_keys=True): _composite(strategy, group, c, holdout) for c in combos}
    any_comp = next(iter(comps.values()))[0]
    last_year = any_comp.index[-1].year
    if holdout:
        years = list(range(data.HOLDOUT_START.year, last_year + 1))
    else:
        years = list(range(cfg["first_test_year"], data.HOLDOUT_START.year))

    oos, chosen = [], []
    oos_pos: dict[str, list[pd.DataFrame]] = {s: [] for s in data.GROUPS[group]}
    for y in years:
        tr0, tr1 = pd.Timestamp(f"{y - cfg['train_years']}-01-01"), pd.Timestamp(f"{y}-01-01")
        best_key, best = None, -np.inf
        for k, (comp, _) in comps.items():
            s = _sharpe(comp[(comp.index >= tr0) & (comp.index < tr1)])
            if s > best:
                best_key, best = k, s
        if best_key is None:  # 训练期数据不足（如刚上市），用中间那组参数
            best_key = json.dumps(combos[len(combos) // 2], sort_keys=True)
        comp, runs = comps[best_key]
        mask = comp.index.year == y
        if not mask.any():
            continue
        oos.append(comp[mask])
        chosen.append({"year": y, "params": json.loads(best_key)})
        for s, r in runs.items():
            oos_pos[s].append(r[r.index.year == y])

    ppy = data.PERIODS_PER_YEAR[group]
    oos_ret = pd.concat(oos)
    n_trades, per_asset = 0, {}
    for s, parts in oos_pos.items():
        if not parts:
            continue
        r = pd.concat(parts).dropna(subset=["ret"])
        if not len(r):
            continue
        t = trades(r["pos"], r["ret"])
        n_trades += len(t)
        m = metrics(r["ret"], ppy, r["pos"])
        per_asset[s] = {k: m.get(k) for k in ("cagr", "max_dd", "sharpe", "trades", "exposure")}
    m = metrics(oos_ret, ppy)
    m["trades"] = n_trades
    m["exposure"] = float(np.mean([v["exposure"] for v in per_asset.values()])) if per_asset else None
    return {"strategy": strategy, "group": group, "holdout": holdout, "period": [str(oos_ret.index[0].date()), str(oos_ret.index[-1].date())],
            "metrics": m, "chosen_params": chosen, "per_asset": per_asset, "returns": oos_ret}


def evaluate(strategy: str, group: str, holdout: bool = False) -> dict[str, Any]:
    if holdout:
        data.log_holdout_access(f"评测 {strategy} @ {group}")
    res = walk_forward(strategy, group, holdout)
    bench = walk_forward("buy_hold", group, holdout)
    m, b = res["metrics"], bench["metrics"]
    causal = causality_check(strategy, group)
    guard = {
        "not_benchmark": strategy != "buy_hold",
        "causal": causal["ok"],
        "min_trades": m["trades"] >= MIN_TRADES,
        "max_dd": abs(m["max_dd"]) <= MAX_DD_VS_BENCH * abs(b["max_dd"]) + 1e-12,
        "min_cagr": b["cagr"] <= 0 or m["cagr"] >= MIN_CAGR_VS_BENCH * b["cagr"],
    }
    score = m["calmar"] / b["calmar"] if b["calmar"] > 0 else None
    out = {k: v for k, v in res.items() if k != "returns"}
    out.update({
        "benchmark": b,
        "score": score,  # 主指标：样本外 Calmar / 基准 Calmar
        "sharpe_vs_bench": m["sharpe"] - b["sharpe"],
        "guardrails": guard,
        "passes": all(guard.values()),
        "causality_detail": causal.get("detail"),
        "param_changes": sum(1 for a, c in zip(res["chosen_params"], res["chosen_params"][1:]) if a["params"] != c["params"]),
    })
    return out


# ---------------------------------------------------------------- 记分牌
def load_scoreboard() -> dict[str, Any]:
    if SCOREBOARD.exists():
        return json.loads(SCOREBOARD.read_text())
    return {"groups": {}, "experiments": 0}


def judge(result: dict[str, Any]) -> dict[str, Any]:
    """与现任冠军比较，给出是否应采纳的结论（不修改记分牌）。"""
    sb = load_scoreboard()
    champ = sb["groups"].get(result["group"])
    if not result["passes"]:
        failed = [k for k, v in result["guardrails"].items() if not v]
        return {"accept": False, "reason": f"未通过护栏：{', '.join(failed)}"}
    if result["score"] is None:
        return {"accept": False, "reason": "基准 Calmar ≤ 0，无法计算主指标"}
    if not champ:
        return {"accept": True, "reason": "该组还没有冠军"}
    need = champ["score"] * (1 + ACCEPT_MARGIN)
    if result["score"] >= need:
        return {"accept": True, "reason": f"主指标 {result['score']:.3f} ≥ 冠军 {champ['score']:.3f} × {1 + ACCEPT_MARGIN}"}
    return {"accept": False, "reason": f"主指标 {result['score']:.3f} 未超过冠军 {champ['score']:.3f} × {1 + ACCEPT_MARGIN}（= {need:.3f}）"}


def crown(result: dict[str, Any], note: str = "") -> None:
    """把结果记为该组新冠军（只应在 judge 同意、且人工审核 PR 时生效）。"""
    sb = load_scoreboard()
    sb["groups"][result["group"]] = {
        "strategy": result["strategy"], "score": result["score"], "metrics": result["metrics"],
        "benchmark": {k: result["benchmark"][k] for k in ("cagr", "max_dd", "calmar", "sharpe")},
        "crowned_at": f"{datetime.now(timezone.utc):%Y-%m-%d}", "note": note,
    }
    SCOREBOARD.parent.mkdir(parents=True, exist_ok=True)
    SCOREBOARD.write_text(json.dumps(sb, ensure_ascii=False, indent=1))


def bump_experiments(n: int = 1) -> int:
    sb = load_scoreboard()
    sb["experiments"] = sb.get("experiments", 0) + n
    SCOREBOARD.parent.mkdir(parents=True, exist_ok=True)
    SCOREBOARD.write_text(json.dumps(sb, ensure_ascii=False, indent=1))
    return sb["experiments"]
