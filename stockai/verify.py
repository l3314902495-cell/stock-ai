"""报告数字核对：检查 AI 报告里的数字能否在本次工具返回的数据里找到。

找不到不一定是编造（可能是 AI 自己推算、或来自网络搜索），但值得人工看一眼。
跳过的内容：日期/时间、年份、周期写法（15m、4h、MA20、RSI14）、时间跨度（3 周、1-3 个月）、
列表序号、小于等于 12 的整数、最后的 json 记录块。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

_UNIT = {"万亿": 1e12, "千亿": 1e11, "亿": 1e8, "万": 1e4, "T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3,
         "bn": 1e9, "tn": 1e12, "mn": 1e6}

# 数字 + 可选单位
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"                    # 前面不能紧跟字母/数字（排除 MA20、Q3、FY2026）
    r"([+\-−]?\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[+\-−]?\$?\d+(?:\.\d+)?)"
    r"\s?(万亿|千亿|亿|万|bn|tn|mn|[TBMK](?![A-Za-z])|%)?"
)
_SKIP_AFTER = re.compile(r"^\s?(?:年|月|日|号|天|周|个月|季度|季|小时|分钟|次|根|倍ATR|个|条|家|只|:\d|[mhdw](?![A-Za-z]))")
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?|\d{1,2}[-/月]\d{1,2}日?|\d{1,2}:\d{2}")
_JSON_BLOCK = re.compile(r"```json.*?```", re.S)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "").replace("$", "").replace("−", "-"))
    except ValueError:
        return None


def numbers_in_text(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text):
        v = _to_float(m.group(1))
        if v is None:
            continue
        unit = m.group(2)
        if unit and unit != "%":
            v *= _UNIT[unit]
        out.append(v)
    return out


def collect_values(obj: Any, acc: list[float] | None = None) -> list[float]:
    """递归收集工具输出中的所有数字（包括字符串里的 "4.31T"、新闻标题里的数字）。"""
    acc = [] if acc is None else acc
    if isinstance(obj, bool) or obj is None:
        return acc
    if isinstance(obj, (int, float)):
        if not (isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj))):
            acc.append(float(obj))
    elif isinstance(obj, str):
        acc.extend(numbers_in_text(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_values(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_values(v, acc)
    return acc


def _decimals(raw: str) -> int:
    raw = raw.replace(",", "")
    return len(raw.split(".")[1]) if "." in raw else 0


def _matches(x: float, decimals: int, pool: list[float]) -> bool:
    ax = abs(x)
    tol_round = 0.5 * 10 ** (-decimals) * 1.001 + 1e-12
    for v in pool:
        for cand in (v, v * 100, v / 100):  # 比例 <-> 百分比
            ac = abs(cand)
            if abs(ac - ax) <= tol_round or (ac and abs(ac - ax) / ac <= 0.006):
                return True
    return False


def check(report: str, tool_outputs: Iterable[str | dict], extra_texts: Iterable[str] = ()) -> dict[str, Any]:
    pool: list[float] = []
    for o in tool_outputs:
        try:
            data = json.loads(o) if isinstance(o, str) else o
        except (json.JSONDecodeError, TypeError):
            data = o
        collect_values(data, pool)
    for t in extra_texts:
        pool.extend(numbers_in_text(t))
    # 去重 + 排序加速不是必要的，数量级通常只有几千
    pool = list({round(v, 10) for v in pool})

    body = _JSON_BLOCK.sub(" ", report)
    body = _CODE_BLOCK.sub(" ", body)
    body = _DATE_RE.sub(" ", body)
    checked, verified, unverified = 0, 0, []
    for line in body.splitlines():
        line_clean = re.sub(r"^\s*(?:[-*>]|\d+[.、)])\s*", "", line)  # 列表序号
        for m in _NUM_RE.finditer(line_clean):
            raw, unit = m.group(1), m.group(2)
            after = line_clean[m.end():]
            if unit is None and _SKIP_AFTER.match(after):
                continue
            v = _to_float(raw)
            if v is None:
                continue
            if unit is None and float(v).is_integer() and (abs(v) <= 12 or 1990 <= v <= 2100):
                continue
            val = v * _UNIT[unit] if unit and unit != "%" else v
            checked += 1
            if _matches(val, _decimals(raw) if not unit or unit == "%" else 2, pool) or (
                    unit and unit != "%" and _matches(val, 0, pool)):
                verified += 1
            else:
                s, e = max(0, m.start() - 18), min(len(line_clean), m.end() + 12)
                unverified.append({"text": m.group(0).strip(), "context": line_clean[s:e].strip()})
    return {
        "checked": checked,
        "verified": verified,
        "ratio": round(verified / checked, 3) if checked else None,
        "unverified": unverified[:40],
    }
