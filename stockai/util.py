"""通用小工具：数值清洗、让工具输出更紧凑（省 token）。"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def num(x: Any, digits: int = 5) -> Any:
    """把数字保留有效位数；NaN/inf -> None。"""
    if x is None:
        return None
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    if math.isnan(f) or math.isinf(f):
        return None
    if f == 0:
        return 0.0
    mag = int(math.floor(math.log10(abs(f))))
    nd = max(digits - 1 - mag, 0)
    r = round(f, nd)
    return int(r) if nd == 0 and abs(r) >= 1e4 else r


def pct(x: Any, digits: int = 2) -> Any:
    """小数 -> 百分比数值（0.1234 -> 12.34）。"""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f * 100, digits)


def clean(obj: Any) -> Any:
    """递归清洗成 JSON 友好的结构。"""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.strftime("%Y-%m-%d %H:%M") if (obj.hour or obj.minute) else obj.strftime("%Y-%m-%d")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (float, int, np.floating, np.integer)) and not isinstance(obj, bool):
        return num(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def big(x: Any) -> Any:
    """大数字转成易读字符串：1.23e12 -> '1.23T'。"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(f) >= div:
            return f"{f / div:.2f}{unit}"
    return f"{f:.2f}"


def now_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
