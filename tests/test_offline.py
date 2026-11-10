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
