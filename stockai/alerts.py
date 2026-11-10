"""价位提醒：手动添加，或从 AI 分析结论自动生成（入场区、止损、目标、长线合理价值下沿）。

`stockai alerts watch` 或 `stockai web --alerts` 在后台定时检查，触发后推送。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .config import expand, get_config

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(expand(get_config()["journal"]["path"]), check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, asset_type TEXT, cond TEXT, price REAL, note TEXT,
        source TEXT, created_at TEXT, active INTEGER DEFAULT 1, triggered_at TEXT, triggered_price REAL)""")
    return conn


def add(symbol: str, asset_type: str, cond: str, price: float, note: str = "", source: str = "manual") -> int:
    if cond not in ("above", "below"):
        raise ValueError("cond 只能是 above / below")
    with _lock:
        conn = _db()
        cur = conn.execute(
            "INSERT INTO alerts (symbol, asset_type, cond, price, note, source, created_at) VALUES (?,?,?,?,?,?,?)",
            (symbol.upper(), asset_type, cond, float(price), note, source, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        return int(cur.lastrowid)


def remove(id_: int) -> None:
    with _lock:
        conn = _db()
        conn.execute("DELETE FROM alerts WHERE id=?", (id_,))
        conn.commit()


def list_alerts(active_only: bool = False) -> list[dict[str, Any]]:
    cols = ["id", "symbol", "asset_type", "cond", "price", "note", "source", "created_at", "active", "triggered_at",
            "triggered_price"]
    q = f"SELECT {', '.join(cols)} FROM alerts" + (" WHERE active=1" if active_only else "") + " ORDER BY id DESC"
    with _lock:
        return [dict(zip(cols, r)) for r in _db().execute(q).fetchall()]


def from_analysis(journal_id: int, symbol: str, asset_type: str, summary: dict[str, Any], price_now: float | None) -> list[int]:
    """根据 AI 结论生成提醒。方向根据当前价自动判断（价格在上方就是 below 提醒，反之 above）。"""
    ids: list[int] = []
    src = f"analysis#{journal_id}"

    def mk(level, note):
        if not isinstance(level, (int, float)) or not price_now:
            return
        cond = "below" if price_now > level else "above"
        ids.append(add(symbol, asset_type, cond, float(level), note, src))

    st = summary.get("short_term") or {}
    if st.get("bias") in ("看多", "看空"):
        entry = st.get("entry")
        if isinstance(entry, (int, float)):
            entry = [entry]
        if isinstance(entry, list) and entry and all(isinstance(x, (int, float)) for x in entry) and price_now:
            lo, hi = min(entry), max(entry)
            if not lo <= price_now <= hi:  # 已经在入场区内就不用提醒
                mk(hi if price_now > hi else lo, f"进入短线入场区 {entry}（{st['bias']}）")
        mk(st.get("stop"), "触及短线止损")
        for i, t in enumerate(st.get("targets") or [], 1):
            mk(t, f"到达短线目标{i}")
    lt = summary.get("long_term") or {}
    fv = lt.get("fair_value")
    if isinstance(fv, list) and fv and isinstance(fv[0], (int, float)) and price_now and price_now > fv[0]:
        mk(fv[0], f"跌到长线合理价值区间下沿 {fv}")
    return ids


def _price(symbol: str, asset_type: str) -> float | None:
    from . import tools

    import json

    out, err = tools.call_tool("get_quote", {"symbol": symbol if asset_type == "crypto" else f"stock:{symbol}"})
    if err:
        return None
    return json.loads(out).get("quote", {}).get("price")


def check_once(on_trigger: Callable[[dict, float], None] | None = None) -> list[dict[str, Any]]:
    from . import notify

    fired = []
    active = list_alerts(active_only=True)
    prices: dict[tuple[str, str], float | None] = {}
    for a in active:
        key = (a["symbol"], a["asset_type"])
        if key not in prices:
            prices[key] = _price(*key)
        p = prices[key]
        if p is None:
            continue
        if (a["cond"] == "above" and p >= a["price"]) or (a["cond"] == "below" and p <= a["price"]):
            with _lock:
                conn = _db()
                conn.execute("UPDATE alerts SET active=0, triggered_at=?, triggered_price=? WHERE id=?",
                             (datetime.now(timezone.utc).isoformat(timespec="seconds"), p, a["id"]))
                conn.commit()
            title = f"{a['symbol']} {'≥' if a['cond'] == 'above' else '≤'} {a['price']}"
            body = f"现价 {p}。{a['note'] or ''}（{a['source']}）"
            a.update(triggered_price=p, notify=notify.send(title, body))
            fired.append(a)
            if on_trigger:
                on_trigger(a, p)
    return fired


def watch(interval: int = 300, on_trigger: Callable[[dict, float], None] | None = None,
          stop: threading.Event | None = None) -> None:
    while not (stop and stop.is_set()):
        try:
            check_once(on_trigger)
        except Exception:
            pass
        if stop:
            stop.wait(interval)
        else:
            time.sleep(interval)
