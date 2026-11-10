"""离线单元测试：只测计算逻辑，不访问网络。运行：pytest -q"""
import numpy as np
import pandas as pd

from stockai.analytics import technicals as ta
from stockai.analytics import valuation as val
from stockai.journal import extract_json
from stockai.util import num, pct


def _df(n=300, seed=0, drift=0.001):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    open_ = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": rng.uniform(1e6, 2e6, n)}, index=idx)


def test_rsi_bounds():
    r = ta.rsi(_df()["Close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_rsi_uptrend_high():
    s = pd.Series(np.linspace(10, 50, 100))
    assert ta.rsi(s).iloc[-1] > 90


def test_summarize_shape():
    out = ta.summarize(_df(), "1d")
    for k in ("price", "rsi14", "macd", "atr14", "levels", "volume_profile", "signals", "recent_bars"):
        assert k in out
    assert all(p < out["price"] for p, _ in out["levels"]["support"])
    assert all(p > out["price"] for p, _ in out["levels"]["resistance"])


def test_performance_vs_benchmark():
    a, b = _df(seed=1), _df(seed=2)
    out = ta.performance(a, b, 252)
    assert "return_%" in out and "beta_1y" in out


def test_dcf_monotonic_in_growth():
    lo = val.dcf(100, 10, 0, 50, [{"growth_5y": 0.05, "terminal_growth": 0.03, "discount_rate": 0.09}])
    hi = val.dcf(100, 10, 0, 50, [{"growth_5y": 0.15, "terminal_growth": 0.03, "discount_rate": 0.09}])
    assert hi["scenarios"][0]["fair_value_per_share"] > lo["scenarios"][0]["fair_value_per_share"]


def test_reverse_dcf_roundtrip():
    sc = [{"growth_5y": 0.12, "terminal_growth": 0.03, "discount_rate": 0.09}]
    fair = val.dcf(100, 10, 0, 1, sc)["scenarios"][0]["fair_value_per_share"]
    implied = val.reverse_dcf(100, 10, 0, fair)["implied_fcf_growth_y1_5_%"]
    assert abs(implied - 12) < 0.1


def test_dcf_negative_fcf():
    assert "error" in val.dcf(-5, 10, 0, 50, [{"growth_5y": 0.1, "terminal_growth": 0.03, "discount_rate": 0.09}])


def test_trade_plan_r_multiple():
    p = val.trade_plan(100, 95, [110, 115], atr=4, account_size=10000, risk_pct=1)
    assert p["side"] == "long"
    assert p["targets"][0]["r_multiple"] == 2.0
    assert p["position"]["quantity"] == 20
    short = val.trade_plan(100, 105, [90], atr=4)
    assert short["side"] == "short" and short["targets"][0]["r_multiple"] == 2.0


def test_extract_json_last_block():
    md = "x\n```json\n{\"a\": 1}\n```\ny\n```json\n{\"symbol\": \"NVDA\", \"price\": 1.5}\n```"
    assert extract_json(md) == {"symbol": "NVDA", "price": 1.5}
    assert extract_json("no json") is None


def test_num_pct():
    assert num(float("nan")) is None
    assert num(0.000012345678) == 0.000012346
    assert pct(0.12345) == 12.35


# ---------------------------------------------------------------- review 回放逻辑
from stockai.journal import _horizon_days, evaluate_plan  # noqa: E402
from stockai.verify import check  # noqa: E402


def _bars(rows, start="2026-01-01 00:00"):
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


T0 = pd.Timestamp("2025-12-31 23:00", tz="UTC")
PLAN = {"bias": "看多", "entry": [95, 97], "stop": 90, "targets": [110, 120], "horizon": "1-2周"}


def test_horizon_days():
    assert _horizon_days("1-3周") == 21 and _horizon_days("5天") == 5 and _horizon_days("2个月") == 60
    assert _horizon_days(None) is None


def test_plan_not_filled_when_price_never_reaches_entry():
    bars = _bars([[100, 112, 99, 111]])  # 直接涨到目标，但从未回到 95-97 入场区
    r = evaluate_plan(PLAN, bars, 100, T0, now=T0 + pd.Timedelta(days=1))
    assert r["outcome"] == "等待入场"
    r = evaluate_plan(PLAN, bars, 100, T0, now=T0 + pd.Timedelta(days=30))
    assert r["outcome"] == "未触发入场（已过期）"


def test_plan_filled_then_target():
    bars = _bars([[100, 100, 96, 97], [97, 105, 96, 104], [104, 111, 103, 110]])
    r = evaluate_plan(PLAN, bars, 100, T0, now=T0 + pd.Timedelta(days=1))
    assert r["outcome"] == "到达目标1" and r["r_multiple"] == round((110 - 96) / 6, 2)


def test_plan_filled_then_stop_and_short_side():
    bars = _bars([[96, 97, 95, 96], [96, 96, 89, 90]])
    assert evaluate_plan(PLAN, bars, 100, T0)["outcome"] == "止损"
    short = {"bias": "看空", "entry": 100, "stop": 105, "targets": [90], "horizon": "1周"}
    bars = _bars([[100, 101, 99, 100], [100, 100, 89, 90]])
    r = evaluate_plan(short, bars, 100, T0)
    assert r["outcome"] == "到达目标1" and r["r_multiple"] == 2.0


def test_plan_expires_at_close():
    bars = _bars([[96, 97, 95, 96]] + [[100, 101, 99, 100]] * 200)  # 入场后一直横盘，超过 1 周
    r = evaluate_plan({**PLAN, "horizon": "1周"}, bars, 96, T0)
    assert r["outcome"] == "到期平仓" and r["r_multiple"] == round((100 - 96) / 6, 2)


def test_verify_units_and_skips():
    tools = ['{"price": 178.67, "market_cap": "4.31T", "oi": "2.61B", "ratio": 0.92757}']
    rep = "现价 178.67，市值 4.31 万亿，持仓 26.1 亿，流通率 92.8%。MA20、RSI14、4h、1-3周、2026-11-17 不算。编造 49.5"
    v = check(rep, tools)
    assert v["checked"] == 5 and v["verified"] == 4 and v["unverified"][0]["text"] == "49.5"


def test_cache_skips_nocache(tmp_path):
    from stockai import cache, config

    (tmp_path / "c.yaml").write_text(f'{{"cache": {{"path": "{tmp_path / "c.db"}"}}}}')
    config.load_config(str(tmp_path / "c.yaml"))
    cache._conn = None
    calls = []

    @cache.cached(ttl=60)
    def f(x):
        calls.append(x)
        return {"v": x, "_nocache": True} if x == "bad" else {"v": x}

    assert f("bad") == {"v": "bad"} and f("bad") == {"v": "bad"}
    assert f("ok") == {"v": "ok"} and f("ok") == {"v": "ok"}
    assert calls == ["bad", "bad", "ok"]
    cache._conn = None
