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

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import journal, symbols, tools
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


@app.get("/api/analyze/stream")
async def analyze_stream(symbol: str, mode: str = "both", provider: str | None = None,
                         question: str | None = Query(None)):
    from ..agent import analyze
    from ..llm.errors import explain

    q: queue.Queue = queue.Queue()

    def on_event(kind: str, data: dict[str, Any]):
        q.put({"type": kind, **data})

    def worker():
        try:
            res = analyze(symbol, mode, provider or None, question or None, save=True, on_event=on_event)
            q.put({"type": "done", "text": res.turn.text, "model": res.turn.model, "usage": res.turn.usage,
                   "warnings": res.turn.warnings, "journal_id": res.journal_id, "tool_calls": len(res.turn.tool_calls)})
        except Exception as e:
            q.put({"type": "error", "text": explain(e)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
