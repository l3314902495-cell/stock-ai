"""分析日志：保存每次 AI 结论 + 工具原始数据，之后回看 AI 判断准不准。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .config import expand, get_config
from .util import num, pct

_lock = threading.Lock()
_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT", "created_at": "TEXT", "symbol": "TEXT", "asset_type": "TEXT",
    "mode": "TEXT", "provider": "TEXT", "model": "TEXT", "price": "REAL", "summary_json": "TEXT", "report": "TEXT",
    "usage_json": "TEXT", "tool_log_json": "TEXT", "verify_json": "TEXT", "cost_json": "TEXT", "critic_notes": "TEXT",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(expand(get_config()["journal"]["path"]), check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS analyses (" + ", ".join(f"{k} {v}" for k, v in _COLUMNS.items()) + ")")
    have = {r[1] for r in conn.execute("PRAGMA table_info(analyses)")}
    for k, v in _COLUMNS.items():  # 旧库自动加列
        if k not in have:
            conn.execute(f"ALTER TABLE analyses ADD COLUMN {k} {v}")
    conn.commit()
    return conn


def extract_json(report: str) -> dict[str, Any] | None:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", report, re.S)
    for b in reversed(blocks):
        try:
            return json.loads(b)
        except json.JSONDecodeError:
            continue
    return None


def save(symbol: str, asset_type: str, mode: str, provider: str, model: str, report: str,
         usage: dict[str, Any], tool_log: list[dict] | None = None, verify: dict | None = None,
         cost: dict | None = None, critic_notes: str | None = None) -> int:
    summary = extract_json(report) or {}
    price = summary.get("price")
    with _lock:
        conn = _db()
        cur = conn.execute(
            "INSERT INTO analyses (created_at, symbol, asset_type, mode, provider, model, price, summary_json, report,"
            " usage_json, tool_log_json, verify_json, cost_json, critic_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), symbol, asset_type, mode, provider, model,
             price if isinstance(price, (int, float)) else None, json.dumps(summary, ensure_ascii=False),
             report, json.dumps(usage), json.dumps(tool_log or [], ensure_ascii=False),
             json.dumps(verify or {}, ensure_ascii=False), json.dumps(cost or {}), critic_notes),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_recent(limit: int = 30) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute(
            "SELECT id, created_at, symbol, asset_type, mode, provider, model, price, summary_json, verify_json, cost_json"
            " FROM analyses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    cols = ["id", "created_at", "symbol", "asset_type", "mode", "provider", "model", "price", "summary", "verify", "cost"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        for k in ("summary", "verify", "cost"):
            d[k] = json.loads(d[k] or "{}")
        v = d.pop("verify")
        d["verify_ratio"] = v.get("ratio")
        out.append(d)
    return out


def get(id_: int, with_tools: bool = False) -> dict[str, Any] | None:
    cols = ["id", "created_at", "symbol", "asset_type", "mode", "provider", "model", "price", "summary_json", "report",
            "usage_json", "verify_json", "cost_json", "critic_notes"] + (["tool_log_json"] if with_tools else [])
    with _lock:
        r = _db().execute(f"SELECT {', '.join(cols)} FROM analyses WHERE id=?", (id_,)).fetchone()
    if not r:
        return None
    d = dict(zip(cols, r))
    for k in [c for c in cols if c.endswith("_json")]:
        d[k[:-5]] = json.loads(d.pop(k) or ("[]" if k == "tool_log_json" else "{}"))
    return d


# ---------------------------------------------------------------- 事后评估
def _horizon_days(h: Any) -> int | None:
    """'1-3周' -> 21，'5天' -> 5，'2个月' -> 60。取上限。"""
    if not isinstance(h, str):
        return None
    m = re.findall(r"(\d+(?:\.\d+)?)\s*(?:-|~|至|到)?\s*(\d+(?:\.\d+)?)?\s*(天|日|周|个月|月)", h)
    if not m:
        return None
    a, b, unit = m[-1]
    n = float(b or a)
    return int(n * {"天": 1, "日": 1, "周": 7, "个月": 30, "月": 30}[unit])


def evaluate_plan(st: dict[str, Any], bars: pd.DataFrame, price_then: float | None,
                  created: pd.Timestamp, now: pd.Timestamp | None = None) -> dict[str, Any]:
    """短线计划回放：先看是否进入入场区间，再看先触及止损还是目标。bars 为分析之后的 K 线。"""
    bias, stop, targets = st.get("bias"), st.get("stop"), [t for t in (st.get("targets") or []) if t]
    if bias not in ("看多", "看空") or not stop or not targets:
        return {"outcome": "无交易计划"}
    long_ = bias == "看多"
    entry = st.get("entry")
    if isinstance(entry, (int, float)):
        lo = hi = float(entry)
    elif isinstance(entry, list) and entry and all(isinstance(x, (int, float)) for x in entry):
        lo, hi = float(min(entry)), float(max(entry))
    elif price_then:
        lo = hi = float(price_then)
    else:
        return {"outcome": "缺少入场价"}
    mid = (lo + hi) / 2
    risk = abs(mid - stop)
    days = _horizon_days(st.get("horizon"))
    deadline = created + pd.Timedelta(days=days) if days else None
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    res: dict[str, Any] = {"entry_zone": [num(lo), num(hi)], "horizon_days": days}

    filled_at = None
    if price_then and lo * 0.999 <= price_then <= hi * 1.001:
        filled_at = created
    after = bars
    for ts, bar in after.iterrows():
        if deadline is not None and ts > deadline and filled_at is None:
            break
        if filled_at is None:
            if bar["Low"] <= hi and bar["High"] >= lo:
                filled_at = ts
            else:
                continue
        # 已入场：检查止损/目标（入场那根 K 线也算）
        hit_stop = bar["Low"] <= stop if long_ else bar["High"] >= stop
        hit_t1 = bar["High"] >= targets[0] if long_ else bar["Low"] <= targets[0]
        if hit_stop and hit_t1:
            res.update(outcome="同一根K线触及止损和目标（无法判断）", filled_at=str(filled_at), closed_at=str(ts))
            return res
        if hit_stop:
            res.update(outcome="止损", r_multiple=-1.0, filled_at=str(filled_at), closed_at=str(ts))
            return res
        if hit_t1:
            r = abs(targets[0] - mid) / risk if risk else None
            out = "到达目标1"
            rest = after[after.index >= ts]
            if len(targets) > 1 and ((rest["High"] >= targets[1]).any() if long_ else (rest["Low"] <= targets[1]).any()):
                out, r = "到达目标2", abs(targets[1] - mid) / risk if risk else None
            res.update(outcome=out, r_multiple=round(r, 2) if r is not None else None, filled_at=str(filled_at), closed_at=str(ts))
            return res
        if deadline is not None and ts > deadline:
            px = float(bar["Close"])
            r = ((px - mid) if long_ else (mid - px)) / risk if risk else None
            res.update(outcome="到期平仓", r_multiple=round(r, 2) if r is not None else None, filled_at=str(filled_at), closed_at=str(ts))
            return res
    if filled_at is None:
        expired = deadline is not None and now > deadline
        res["outcome"] = "未触发入场（已过期）" if expired else "等待入场"
        return res
    res.update(outcome="持仓中", filled_at=str(filled_at))
    return res


_FINAL = ("止损", "到达目标2", "到期平仓", "未触发入场（已过期）", "同一根K线触及止损和目标（无法判断）")
_STEP = {"1h": pd.Timedelta(hours=1), "4h": pd.Timedelta(hours=4), "1d": pd.Timedelta(days=1)}


def _is_final(plan: dict[str, Any], st: dict[str, Any], created: pd.Timestamp) -> bool:
    out = plan.get("outcome")
    if out in _FINAL:
        return True
    if out == "到达目标1":  # 只有一个目标，或已过持仓期限，目标2 不会再算了
        days = plan.get("horizon_days")
        return len(st.get("targets") or []) < 2 or (days is not None and pd.Timestamp.now(tz="UTC") > created + pd.Timedelta(days=days))
    return False


def bars_since(sym: dict[str, Any], created: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    """取覆盖"分析时间至今"的 K 线：越久远的分析用越粗的周期，保证不缺数据。"""
    from .data import crypto, us_stock

    age_h = (pd.Timestamp.now(tz="UTC") - created).total_seconds() / 3600 + 24
    if sym["asset_type"] == "crypto":
        if age_h <= 4800:
            iv, n = "1h", age_h
        elif age_h <= 4 * 3000:
            iv, n = "4h", age_h / 4
        else:
            iv, n = "1d", age_h / 24
        limit = int(-(-n // 240) * 240)  # 向上取整到 240 的倍数，便于命中缓存
        df = crypto.history(sym["symbol"], iv, min(limit, 5040))
    else:
        iv = "1h" if age_h <= 700 * 24 else "1d"
        df = us_stock.history(sym["symbol"], iv, "730d" if iv == "1h" else "max")
    idx = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
    return df.set_axis(idx), iv


def _plan_cache_get(id_: int) -> dict[str, Any] | None:
    with _lock:
        conn = _db()
        conn.execute("CREATE TABLE IF NOT EXISTS plan_results (analysis_id INTEGER PRIMARY KEY, result_json TEXT)")
        r = conn.execute("SELECT result_json FROM plan_results WHERE analysis_id=?", (id_,)).fetchone()
    return json.loads(r[0]) if r else None


def _plan_cache_put(id_: int, plan: dict[str, Any]) -> None:
    with _lock:
        conn = _db()
        conn.execute("CREATE TABLE IF NOT EXISTS plan_results (analysis_id INTEGER PRIMARY KEY, result_json TEXT)")
        conn.execute("INSERT OR REPLACE INTO plan_results VALUES (?, ?)", (id_, json.dumps(plan, ensure_ascii=False)))
        conn.commit()


def evaluate(entry: dict[str, Any], prices: dict[tuple, float] | None = None) -> dict[str, Any]:
    """评估一条历史分析。已结束的短线计划结果会缓存，之后只需取一次现价。prices 用于批量评估时共享现价。"""
    from . import symbols
    from .tools import ohlcv

    created = pd.Timestamp(entry["created_at"])
    if created.tz is None:
        created = created.tz_localize("UTC")
    key = (entry["symbol"], entry["asset_type"])
    sym = symbols.resolve(entry["symbol"] if entry["asset_type"] == "crypto" else f"stock:{entry['symbol']}")
    st = (entry.get("summary") or {}).get("short_term") or {}
    price_then = entry.get("price")
    plan = _plan_cache_get(entry["id"]) if entry.get("id") is not None and st else None
    res: dict[str, Any] = {"days": round((pd.Timestamp.now(tz="UTC") - created).total_seconds() / 86400, 1)}

    if st and plan is None:
        df, iv = bars_since(sym, created)
        after = df[df.index > created]
        if len(df) and df.index[0] > created + _STEP.get(iv, pd.Timedelta(hours=1)):
            res["note"] = f"K线只覆盖到 {df.index[0]:%Y-%m-%d}，早于此的走势缺失，结果可能不准"
        plan = evaluate_plan(st, after, price_then, created)
        plan["bar_interval"] = iv
        if entry.get("id") is not None and _is_final(plan, st, created) and "note" not in res:
            _plan_cache_put(entry["id"], plan)
        if prices is not None and len(df):
            prices.setdefault(key, float(df["Close"].iloc[-1]))
        if not len(after):
            res.setdefault("note", "分析之后还没有新的K线")

    now_price = prices.get(key) if prices is not None else None
    if now_price is None:
        now_price = float(ohlcv(sym, "1d")["Close"].iloc[-1])
        if prices is not None:
            prices[key] = now_price
    res["now"] = num(now_price)
    if price_then:
        res["return_%"] = pct(now_price / price_then - 1)
    if st:
        res["short_outcome"] = plan.get("outcome")
        res["short_r"] = plan.get("r_multiple")
        res["plan"] = plan
    return res


def stats(limit: int = 500) -> dict[str, Any]:
    """按 模型 / 模式 / 资产类型 汇总历史判断表现。"""
    rows = list_recent(limit)
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    long_by_rating: dict[str, list] = defaultdict(list)
    errors = 0
    prices: dict[tuple, float] = {}  # 同一标的只取一次现价
    for r in rows:
        try:
            ev = evaluate(r, prices)
        except Exception:
            errors += 1
            continue
        keys = [f"模型:{r['model']}", f"模式:{r['mode']}", f"类型:{r['asset_type']}", "全部"]
        out = ev.get("short_outcome")
        for k in keys:
            g = groups[k]
            g["n"].append(1)
            if ev.get("return_%") is not None:
                g["ret"].append(ev["return_%"])
            if out and out not in ("无交易计划", "缺少入场价"):
                g["plans"].append(out)
                if ev.get("short_r") is not None and out not in ("持仓中",):
                    g["r"].append(ev["short_r"])
                    if out in ("止损", "到达目标1", "到达目标2", "到期平仓"):
                        g["closed_r"].append(ev["short_r"])
        lt = (r.get("summary") or {}).get("long_term") or {}
        if lt.get("rating") and ev.get("return_%") is not None:
            long_by_rating[lt["rating"]].append(ev["return_%"])

    def summarize(g):
        plans = g["plans"]
        closed = [p for p in plans if p in ("止损", "到达目标1", "到达目标2", "到期平仓")]
        wins = [r for r in g["closed_r"] if r > 0]  # 到期平仓但赚钱也算胜
        return {
            "分析数": len(g["n"]),
            "交易计划数": len(plans),
            "已入场": len([p for p in plans if not p.startswith(("等待", "未触发"))]),
            "未触发入场": len([p for p in plans if p.startswith("未触发")]),
            "已结束": len(closed),
            "胜率_%": round(len(wins) / len(g["closed_r"]) * 100, 1) if g["closed_r"] else None,
            "平均R": round(sum(g["r"]) / len(g["r"]), 2) if g["r"] else None,
            "至今平均涨跌_%": round(sum(g["ret"]) / len(g["ret"]), 2) if g["ret"] else None,
        }

    return {
        "groups": {k: summarize(v) for k, v in sorted(groups.items(), key=lambda kv: (kv[0] != "全部", kv[0]))},
        "long_term_by_rating": {k: {"数量": len(v), "至今平均涨跌_%": round(sum(v) / len(v), 2)}
                                for k, v in long_by_rating.items()},
        "evaluate_errors": errors,
        "note": "胜率 = 已结束（止损/到目标/到期）计划中 R>0 的比例；样本少于 30 笔时参考价值有限；平均R：止损记-1，到目标记实际R倍数，到期按收盘价计算",
    }
