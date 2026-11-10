"""本地网页：K 线图 + AI 分析（实时显示工具调用进度）+ 历史报告。

只监听 127.0.0.1，供自己使用。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import time
import uuid
from collections import OrderedDict

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import alerts, journal, symbols, tools
from ..analytics import technicals as ta
from ..config import get_config
from ..llm.base import api_key

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="stock-ai")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/providers")
def providers():
    llm = get_config()["llm"]
    return {"default": llm.get("default"), "providers": [
        {"name": n, "model": c.get("model"), "type": c.get("type", "anthropic"), "ready": bool(api_key(c))}
        for n, c in (llm.get("providers") or {}).items()]}


@app.get("/api/quote")
def quote(symbol: str):
    out, err = tools.call_tool("get_quote", {"symbol": symbol})
    return JSONResponse(json.loads(out), status_code=400 if err else 200)


@app.get("/api/chart")
def chart(symbol: str, interval: str = "1d"):
    try:
        sym = symbols.resolve(symbol)
        df = tools.ohlcv(sym, interval)
    except Exception as e:
        raise HTTPException(400, str(e))
    df = df.iloc[-600:]
    # 图表库按 UTC 显示时间：美股用交易所当地时间（美东），加密用本机时区，换算成"看起来对"的时间戳
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    if sym["asset_type"] == "crypto":
        idx = idx.tz_convert(datetime.now().astimezone().tzinfo)
    ts = [int(t.tz_localize(None).replace(tzinfo=timezone.utc).timestamp()) for t in idx]
    c = df["Close"]

    def line(s):
        return [{"time": t, "value": round(float(v), 6)} for t, v in zip(ts, s) if not np.isnan(v)]

    full = tools.ohlcv(sym, interval)
    mas = {f"ma{n}": line(ta.sma(full["Close"], n).iloc[-len(df):]) for n in (20, 50, 200) if len(full) >= n}
    levels = ta.swing_levels(full.iloc[-250:], max_levels=3)
    return {
        "symbol": sym["symbol"], "name": sym.get("name"), "asset_type": sym["asset_type"], "interval": interval,
        "candles": [{"time": t, "open": float(o), "high": float(h), "low": float(l), "close": float(cl)}
                    for t, o, h, l, cl in zip(ts, df["Open"], df["High"], df["Low"], c)],
        "volume": [{"time": t, "value": float(v), "color": "rgba(38,166,154,.45)" if cl >= o else "rgba(239,83,80,.45)"}
                   for t, v, o, cl in zip(ts, df["Volume"].fillna(0), df["Open"], c)],
        "ma": mas,
        "levels": levels,
    }


def _sse(worker_fn) -> StreamingResponse:
    """在线程里跑 worker_fn(on_event)，把事件以 SSE 推给浏览器。"""
    q: queue.Queue = queue.Queue()

    def on_event(kind: str, data: dict[str, Any]):
        q.put({"type": kind, **data})

    def run():
        from ..llm.errors import explain

        try:
            worker_fn(on_event)
        except Exception as e:
            q.put({"type": "error", "text": explain(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    async def gen():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/analyze/stream")
async def analyze_stream(symbol: str, mode: str = "both", provider: str | None = None,
                         question: str | None = Query(None), critic: bool = False):
    from ..agent import analyze

    def work(on_event):
        res = analyze(symbol, mode, provider or None, question or None, save=True, critic=critic, on_event=on_event)
        on_event("done", {"text": res.turn.text, "model": res.turn.model, "usage": res.turn.usage,
                          "warnings": res.turn.warnings, "journal_id": res.journal_id,
                          "tool_calls": len(res.turn.tool_calls), "verify": res.verify, "cost": res.cost,
                          "critic_notes": res.critic_notes})

    return _sse(work)


@app.get("/api/history")
def history(limit: int = 50):
    return journal.list_recent(limit)


@app.get("/api/history/{id_}/evaluate")
def evaluate(id_: int):
    rows = [r for r in journal.list_recent(500) if r["id"] == id_]
    if not rows:
        raise HTTPException(404)
    try:
        return journal.evaluate(rows[0])
    except Exception as e:
        return {"note": str(e)[:200]}


@app.get("/api/report/{id_}")
def report(id_: int):
    r = journal.get(id_)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/report/{id_}/tools")
def report_tools(id_: int):
    r = journal.get(id_, with_tools=True)
    if not r:
        raise HTTPException(404)
    return r["tool_log"]


@app.get("/api/stats")
def stats():
    return journal.stats()


# ---------------------------------------------------------------- 扫描
@app.get("/api/watchlists")
def watchlists():
    return get_config().get("watchlists") or {}


@app.get("/api/scan")
def scan(symbols_: str | None = Query(None, alias="symbols"), watchlist: str = "default"):
    from .. import scanner

    syms = [x.strip() for x in symbols_.split(",") if x.strip()] if symbols_ else \
        (get_config().get("watchlists") or {}).get(watchlist) or []
    if not syms:
        raise HTTPException(400, "没有要扫描的标的")
    return scanner.scan(syms)


# ---------------------------------------------------------------- 提醒
@app.get("/api/alerts")
def list_alerts(all: bool = True):
    return alerts.list_alerts(active_only=not all)


@app.post("/api/alerts")
def add_alert(body: dict = Body(...)):
    s = symbols.resolve(body["symbol"])
    return {"id": alerts.add(s["symbol"], s["asset_type"], body["cond"], float(body["price"]), body.get("note", ""))}


@app.delete("/api/alerts/{id_}")
def delete_alert(id_: int):
    alerts.remove(id_)
    return {"ok": True}


@app.post("/api/alerts/check")
def check_alerts():
    return alerts.check_once()


_watcher: dict[str, Any] = {}


def start_alert_watcher() -> None:
    if _watcher.get("thread"):
        return
    stop = threading.Event()
    iv = int(get_config().get("alerts", {}).get("interval", 300))
    t = threading.Thread(target=alerts.watch, args=(iv, None, stop), daemon=True)
    t.start()
    _watcher.update(thread=t, stop=stop)


# ---------------------------------------------------------------- 对话
_chats: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_MAX_CHATS = 20


@app.get("/api/chat/stream")
async def chat_stream(msg: str, sid: str | None = None, provider: str | None = None, report_id: int | None = None):
    from ..agent import new_conversation
    from ..prompts import CHAT_HINT
    from ..util import now_str

    if not sid or sid not in _chats:
        sid = sid or uuid.uuid4().hex
        name, conv = new_conversation(provider or None)
        _chats[sid] = {"conv": conv, "lock": threading.Lock(), "first": True, "report_id": report_id, "provider": name}
        while len(_chats) > _MAX_CHATS:
            _chats.popitem(last=False)
    chat = _chats[sid]
    _chats.move_to_end(sid)

    def work(on_event):
        with chat["lock"]:
            text = msg
            if chat["first"]:
                ctx = ""
                if chat["report_id"]:
                    r = journal.get(int(chat["report_id"]))
                    if r:
                        ctx = f"\n\n以下是之前的分析报告（{r['symbol']}，{r['created_at']} UTC），用户可能就此追问：\n{r['report']}\n"
                text = f"当前时间：{now_str()}\n{CHAT_HINT}{ctx}\n\n{msg}"
                chat["first"] = False
            else:
                text = f"（当前时间：{now_str()}）{msg}"
            on_event("session", {"sid": sid, "provider": chat["provider"]})
            turn = chat["conv"].send(text, on_event)
            on_event("done", {"text": turn.text, "model": turn.model, "usage": turn.usage, "warnings": turn.warnings,
                              "tool_calls": len(turn.tool_calls)})

    return _sse(work)
