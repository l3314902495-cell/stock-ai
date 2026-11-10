"""配置加载：config.yaml + .env。

查找顺序：--config 指定路径 > ./config.yaml > ~/.stock-ai/config.yaml > 内置默认值。
密钥只放在 .env / 环境变量里，config.yaml 里只写变量名（api_key_env）。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULTS: dict[str, Any] = {
    "llm": {
        "default": "claude",
        "providers": {
            "claude": {
                "type": "anthropic",
                "model": "claude-opus-5-5",
                "api_key_env": "ANTHROPIC_API_KEY",
                "base_url": None,
                "effort": "high",
                "max_tokens": 16000,
            }
        },
    },
    "network": {"proxy": None},
    "data": {
        "crypto_exchange": "okx",
        "sec_user_agent": "stock-ai personal-research contact@example.com",
    },
    "cache": {"path": "~/.stock-ai/cache.db"},
    "journal": {"path": "~/.stock-ai/journal.db"},
    "reports_dir": "./reports",
    "analysis": {"critic": False},
    "alerts": {"auto_from_analysis": True, "interval": 300},
    "watchlists": {"default": ["NVDA", "AAPL", "MSFT", "TSLA", "SPY", "QQQ", "BTC", "ETH", "SOL"]},
    "notify": {},
}

_config: dict[str, Any] | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "providers":
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict[str, Any]:
    global _config
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path.home() / ".stock-ai" / ".env")

    candidates = [path] if path else ["config.yaml", str(Path.home() / ".stock-ai" / "config.yaml")]
    user_cfg: dict[str, Any] = {}
    for c in candidates:
        if c and Path(c).expanduser().exists():
            with open(Path(c).expanduser(), encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            break
    _config = _deep_merge(DEFAULTS, user_cfg)
    _apply_network(_config)
    return _config


def get_config() -> dict[str, Any]:
    return _config if _config is not None else load_config()


def data_proxy() -> str | None:
    """数据源使用的代理：config.network.proxy 优先，其次环境变量 HTTPS_PROXY。"""
    cfg = get_config()
    return cfg["network"].get("proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def _apply_network(cfg: dict[str, Any]) -> None:
    import logging

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    proxy = cfg["network"].get("proxy")
    if proxy:
        try:
            import yfinance as yf

            yf.set_config(proxy=proxy)
        except Exception:
            pass


def expand(p: str) -> Path:
    path = Path(p).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
