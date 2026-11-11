"""回测与评测系统的测试——这是优化循环的"裁判"，必须可靠。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockai import strategies
from stockai.backtest import data, evaluate as ev
from stockai.backtest.engine import metrics, strategy_returns, trades


def _df(opens, closes):
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"Open": opens, "High": closes, "Low": closes, "Close": closes, "Volume": 1}, index=idx)


def test_engine_executes_next_open():
    # 第 0 天收盘发出信号，第 1 天开盘成交：只吃到第 1 天"开盘→收盘"的涨幅，吃不到隔夜跳空
    df = _df([100, 110, 121], [100, 120, 121])
    sig = pd.Series([1.0, 1.0, 1.0], index=df.index)
    r = strategy_returns(df, sig, cost=0.0)
    assert r["ret"].iloc[0] == 0.0
    assert r["ret"].iloc[1] == pytest.approx(120 / 110 - 1)            # 日内段
    assert r["ret"].iloc[2] == pytest.approx(121 / 120 - 1)            # 隔夜 + 日内（全仓）


def test_engine_costs_and_cash():
    df = _df([100] * 4, [100] * 4)
    sig = pd.Series([1.0, 0.0, 0.0, 0.0], index=df.index)
    cash = pd.Series(0.001, index=df.index)
    r = strategy_returns(df, sig, cost=0.01, cash=cash)
    assert r["ret"].iloc[1] == pytest.approx(-0.01)           # 买入成本，满仓无现金收益
    assert r["ret"].iloc[2] == pytest.approx(-0.01 + 0.001)   # 卖出成本 + 空仓现金收益
    assert r["ret"].iloc[3] == pytest.approx(0.001)


def test_metrics_and_trades():
    ret = pd.Series([0.1, -0.5, 0.2, 0.0], index=pd.date_range("2020", periods=4))
    pos = pd.Series([1, 1, 0, 1.0], index=ret.index)
    m = metrics(ret, 252, pos)
    assert m["max_dd"] == pytest.approx(-0.5)
    assert len(trades(pos, ret)) == 2


def test_holdout_is_locked(tmp_path, monkeypatch):
    df = data.load("SPY")
    assert df.index.max() < data.HOLDOUT_START
    monkeypatch.setattr(data, "HOLDOUT_LOG", tmp_path / "log.md")
    full = data.load("SPY", holdout=True)
    assert full.index.max() >= data.HOLDOUT_START
    data.log_holdout_access("测试")
    assert "测试" in (tmp_path / "log.md").read_text()


def test_causality_check_catches_lookahead():
    @strategies.register("_cheat_test", groups=("stocks",))
    def cheat(df):  # 用明天的收盘价决定今天的仓位
        return (df["Close"].shift(-1) > df["Close"]).astype(float)

    try:
        res = ev.causality_check("_cheat_test", "stocks")
        assert res["ok"] is False and "未来" in res["detail"]
        assert ev.causality_check("sma_trend", "stocks")["ok"] is True
    finally:
        strategies.REGISTRY.pop("_cheat_test", None)


def test_all_registered_strategies_are_causal():
    for name, st in strategies.REGISTRY.items():
        for g in st.groups:
            assert ev.causality_check(name, g)["ok"], f"{name}@{g} 使用了未来数据"


def test_param_limits_enforced():
    with pytest.raises(ValueError):
        strategies.register("_too_many", grid={"a": [1, 2], "b": [1, 2], "c": [1, 2], "d": [1, 2], "e": [1]})(lambda df, **k: None)
    with pytest.raises(ValueError):
        strategies.register("_too_many_combos", grid={"a": list(range(17))})(lambda df, **k: None)
    strategies.REGISTRY.pop("_too_many", None)
    strategies.REGISTRY.pop("_too_many_combos", None)


def test_judge_requires_margin(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "SCOREBOARD", tmp_path / "sb.json")
    base = {"group": "stocks", "strategy": "x", "passes": True, "guardrails": {"causal": True}, "score": 1.0,
            "metrics": {"cagr": 0.1, "max_dd": -0.2, "calmar": 0.5, "sharpe": 1.0},
            "benchmark": {"cagr": 0.1, "max_dd": -0.5, "calmar": 0.2, "sharpe": 0.6}}
    assert ev.judge(base)["accept"]
    ev.crown(base)
    assert not ev.judge({**base, "score": 1.04})["accept"]   # 未超过 5% 门槛
    assert ev.judge({**base, "score": 1.06})["accept"]
    assert not ev.judge({**base, "score": 9.0, "passes": False, "guardrails": {"causal": False}})["accept"]


def test_walk_forward_is_out_of_sample():
    r = ev.walk_forward("sma_trend", "stocks")
    assert r["period"][0].startswith("2008") and r["period"][1] < str(data.HOLDOUT_START.date())
    assert [c["year"] for c in r["chosen_params"]] == list(range(2008, 2025))


def test_reference_helper_is_causal_and_respects_holdout():
    @strategies.register("_btc_filter_test", groups=("crypto",))
    def f(df):
        btc = strategies.reference(df, "BTC-USD")
        return (btc > btc.rolling(200).mean()).astype(float)

    try:
        assert ev.causality_check("_btc_filter_test", "crypto")["ok"]
        df = data.load("ETH-USD")
        assert strategies.reference(df, "BTC-USD").index.max() < data.HOLDOUT_START
    finally:
        strategies.REGISTRY.pop("_btc_filter_test", None)


def test_live_signals_use_live_data_for_reference(monkeypatch):
    from stockai.backtest import live

    calls = []

    def fake_live(symbol, drop_incomplete=True):
        calls.append(symbol)
        df = data.load(symbol).iloc[-400:].copy()
        df.attrs["live"] = True
        return df

    monkeypatch.setattr(live, "live_daily", fake_live)

    @strategies.register("_live_ref_test", groups=("crypto",))
    def f(df):
        btc = strategies.reference(df, "BTC-USD")
        return (btc > btc.rolling(50).mean()).astype(float)

    try:
        res = live.signals("_live_ref_test", "crypto")
        assert len(res["rows"]) == len(data.GROUPS["crypto"]) and all("action" in r for r in res["rows"])
        assert calls.count("BTC-USD") >= len(data.GROUPS["crypto"])  # 参考标的走实时数据
    finally:
        strategies.REGISTRY.pop("_live_ref_test", None)
