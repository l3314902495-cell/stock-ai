"""分析日志：保存每次 AI 结论，之后回看 AI 判断准不准。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .config import expand, get_config
from .util import num, pct

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(expand(get_config()["journal"]["path"]), check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, symbol TEXT, asset_type TEXT, mode TEXT,
        provider TEXT, model TEXT, price REAL, summary_json TEXT, report TEXT, usage_json TEXT)""")
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
         usage: dict[str, Any]) -> int:
    summary = extract_json(report) or {}
    price = summary.get("price")
    with _lock:
        conn = _db()
        cur = conn.execute(
            "INSERT INTO analyses (created_at, symbol, asset_type, mode, provider, model, price, summary_json, report, usage_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), symbol, asset_type, mode, provider, model,
             price if isinstance(price, (int, float)) else None, json.dumps(summary, ensure_ascii=False),
             report, json.dumps(usage)),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_recent(limit: int = 30) -> list[dict[str, Any]]:
    with _lock:
        conn = _db()
        rows = conn.execute(
            "SELECT id, created_at, symbol, asset_type, mode, provider, model, price, summary_json FROM analyses"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    cols = ["id", "created_at", "symbol", "asset_type", "mode", "provider", "model", "price", "summary"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["summary"] = json.loads(d["summary"] or "{}")
        out.append(d)
    return out


def get(id_: int) -> dict[str, Any] | None:
    with _lock:
        r = _db().execute("SELECT id, created_at, symbol, mode, model, report FROM analyses WHERE id=?", (id_,)).fetchone()
    return dict(zip(["id", "created_at", "symbol", "mode", "model", "report"], r)) if r else None


def evaluate(entry: dict[str, Any]) -> dict[str, Any]:
    """用分析之后的K线评估：短线计划是先到止损还是先到目标；长线看至今收益。"""
    from . import symbols
    from .tools import ohlcv

    created = pd.Timestamp(entry["created_at"])
    sym = symbols.resolve(entry["symbol"] if entry["asset_type"] == "crypto" else f"stock:{entry['symbol']}")
    df = ohlcv(sym, "1h")
    idx = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
    df = df.set_axis(idx)
    after = df[df.index > created]
    price_then = entry.get("price")
    now_price = float(df["Close"].iloc[-1])
    res: dict[str, Any] = {"now": num(now_price), "days": round((pd.Timestamp.now(tz="UTC") - created).total_seconds() / 86400, 1)}
    if price_then:
        res["return_%"] = pct(now_price / price_then - 1)
    st = (entry.get("summary") or {}).get("short_term") or {}
    bias, stop, targets = st.get("bias"), st.get("stop"), st.get("targets") or []
    if bias in ("看多", "看空") and stop and targets and len(after):
        long_ = bias == "看多"
        outcome = "进行中"
        for _, bar in after.iterrows():
            hit_stop = bar["Low"] <= stop if long_ else bar["High"] >= stop
            hit_t1 = bar["High"] >= targets[0] if long_ else bar["Low"] <= targets[0]
            if hit_stop and hit_t1:
                outcome = "同一根K线触及止损和目标（不确定）"
                break
            if hit_stop:
                outcome = "止损"
                break
            if hit_t1:
                outcome = "到达目标1"
                if len(targets) > 1 and ((after["High"] >= targets[1]).any() if long_ else (after["Low"] <= targets[1]).any()):
                    outcome = "到达目标2"
                break
        res["short_outcome"] = outcome
    if not len(after):
        res["note"] = "分析之后还没有新的K线"
    return res
