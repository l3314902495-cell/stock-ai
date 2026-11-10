"""按 token 用量估算一次分析的成本。

内置 Claude 官方价格（美元 / 百万 token）。中转站或其他服务商的价格不同，
在 config.yaml 的 provider 里配置 price 覆盖，例如：
  price: {input: 2, output: 8, cache_read: 0.2, cache_write: 2.5, currency: CNY}
"""
from __future__ import annotations

from typing import Any

# cache_write 按 5 分钟缓存（输入价 × 1.25）
CLAUDE_PRICES = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
WEB_SEARCH_PER_1K = 10.0  # 美元 / 1000 次搜索


def price_for(model: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    if cfg.get("price"):
        return cfg["price"]
    if cfg.get("base_url"):  # 中转站价格未知，除非用户配置
        return None
    for k in sorted(CLAUDE_PRICES, key=len, reverse=True):
        if model.startswith(k):
            return {**CLAUDE_PRICES[k], "currency": "USD"}
    return None


def estimate(usage: dict[str, int], model: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    p = price_for(model, cfg)
    if not p:
        return None
    m = 1_000_000
    cost = (usage.get("input_tokens", 0) * p.get("input", 0)
            + usage.get("output_tokens", 0) * p.get("output", 0)
            + usage.get("cache_read_input_tokens", 0) * p.get("cache_read", p.get("input", 0))
            + usage.get("cache_creation_input_tokens", 0) * p.get("cache_write", p.get("input", 0))) / m
    searches = usage.get("web_search_requests", 0)
    if searches and not cfg.get("price"):
        cost += searches * WEB_SEARCH_PER_1K / 1000
    return {"amount": round(cost, 4), "currency": p.get("currency", "USD")}


def fmt(c: dict[str, Any] | None) -> str:
    if not c:
        return "成本未知（可在 provider 里配置 price）"
    sym = {"USD": "$", "CNY": "¥"}.get(c["currency"], c["currency"] + " ")
    return f"约 {sym}{c['amount']:.3f}"
