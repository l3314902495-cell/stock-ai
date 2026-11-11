"""冻结的数据快照 + 验证集锁。

- 数据来自 backtest_data/（仓库内冻结的日线快照，保证每次评测结果可比、可复现）
- 开发期数据截止到 HOLDOUT_START 之前；验证集（HOLDOUT_START 之后）默认读不到。
  只有 load(..., holdout=True) 才能读取，并且每次读取都会写入 optimize/holdout_log.md 留痕。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "backtest_data"
HOLDOUT_LOG = ROOT / "optimize" / "holdout_log.md"

HOLDOUT_START = pd.Timestamp("2025-01-01")

GROUPS: dict[str, list[str]] = {
    # 美股：宽基 + 11 个行业中的 9 个老牌行业 ETF（成立早、无幸存者偏差）
    "stocks": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB"],
    # 加密：市值长期靠前、数据较长的主流币。注意：都是"活下来的"币，存在一定幸存者偏差
    "crypto": ["BTC-USD", "ETH-USD", "LTC-USD", "XRP-USD", "BNB-USD", "ADA-USD", "DOGE-USD"],
}

# 滚动前推：每个测试年用之前 TRAIN_YEARS 年选参数
WALK = {
    "stocks": {"first_test_year": 2008, "train_years": 3},
    "crypto": {"first_test_year": 2018, "train_years": 3},
}

# 每单位换手的交易成本（手续费 0.10% + 滑点 0.05%，单边）
COST = {"stocks": 0.0015, "crypto": 0.0015}
PERIODS_PER_YEAR = {"stocks": 252, "crypto": 365}
# 空仓现金收益：美股组按 13 周国债利率（IRX），加密组保守按 0
CASH_YIELD = {"stocks": "IRX", "crypto": None}


def manifest() -> dict:
    return json.loads((DATA_DIR / "manifest.json").read_text())


@lru_cache(maxsize=64)
def _read(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"{symbol}.csv.gz", index_col="Date", parse_dates=True)
    return df.sort_index()


def log_holdout_access(purpose: str) -> None:
    HOLDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC：{purpose}\n"
    if not HOLDOUT_LOG.exists():
        HOLDOUT_LOG.write_text("# 验证集访问记录\n\n每次读取验证集数据都会自动记录在这里。日常迭代不应出现新记录。\n\n")
    with HOLDOUT_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def load(symbol: str, holdout: bool = False) -> pd.DataFrame:
    """开发期数据（不含验证集）。holdout=True 返回全部数据（调用方负责记录用途）。"""
    df = _read(symbol)
    if holdout:
        return df.copy()
    return df[df.index < HOLDOUT_START].copy()


def cash_daily(group: str, index: pd.DatetimeIndex, holdout: bool = False) -> pd.Series:
    """每个交易日的现金收益率（小数）。"""
    src = CASH_YIELD.get(group)
    if not src:
        return pd.Series(0.0, index=index)
    y = load(src, holdout=holdout)["Close"].reindex(index).ffill().fillna(0.0)
    return y / 100 / PERIODS_PER_YEAR[group]


def group_of(symbol: str) -> str:
    for g, syms in GROUPS.items():
        if symbol in syms:
            return g
    raise KeyError(symbol)
