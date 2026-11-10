"""提醒 / 推送 / 扫描的离线测试。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pandas as pd
import pytest


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    received: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_HOOK", f"http://127.0.0.1:{srv.server_port}/hook")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    (tmp_path / "c.yaml").write_text(json.dumps({
        "journal": {"path": str(tmp_path / "j.db")}, "cache": {"path": str(tmp_path / "c.db")},
        "notify": {"webhook": {"url_env": "TEST_HOOK"}},
    }))
    from stockai import config

    config.load_config(str(tmp_path / "c.yaml"))
    yield received
    srv.shutdown()


def test_alerts_from_analysis_and_trigger(cfg, monkeypatch):
    from stockai import alerts

    summary = {"short_term": {"bias": "看多", "entry": [95, 97], "stop": 90, "targets": [110, 120]},
               "long_term": {"rating": "合理", "fair_value": [80, 120]}}
    ids = alerts.from_analysis(1, "XYZ", "stock", summary, 100)
    rows = {r["note"]: (r["cond"], r["price"]) for r in alerts.list_alerts()}
    assert len(ids) == 5
    assert rows["触及短线止损"] == ("below", 90)
    assert rows["到达短线目标1"] == ("above", 110)
    assert any(k.startswith("进入短线入场区") and v == ("below", 97) for k, v in rows.items())
    assert any(k.startswith("跌到长线合理价值区间下沿") and v == ("below", 80) for k, v in rows.items())

    monkeypatch.setattr(alerts, "_price", lambda s, a: 96.5)  # 进入入场区，但没到止损
    fired = alerts.check_once()
    assert [f["note"][:7] for f in fired] == ["进入短线入场区"]
    assert fired[0]["notify"] == ["webhook: 已发送"]
    assert cfg and "XYZ" in cfg[0]["title"]
    assert len(alerts.list_alerts(active_only=True)) == 4
    assert alerts.check_once() == []  # 已触发的不会重复推送


def test_entry_alert_skipped_when_inside_zone(cfg):
    from stockai import alerts

    ids = alerts.from_analysis(2, "ABC", "crypto", {"short_term": {"bias": "看空", "entry": [99, 101], "stop": 105,
                                                                   "targets": [90]}}, 100)
    notes = [r["note"] for r in alerts.list_alerts() if r["source"] == "analysis#2"]
    assert len(ids) == 2 and not any(n.startswith("进入") for n in notes)


def test_scanner_breakout(cfg, monkeypatch):
    from stockai import scanner

    n = 300
    close = np.r_[np.linspace(100, 110, n - 1) + np.sin(np.arange(n - 1)) * 0.5, 125.0]  # 最后一天突破
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    df = pd.DataFrame({"Open": close * 0.99, "High": close * 1.01, "Low": close * 0.98, "Close": close,
                       "Volume": np.r_[np.full(n - 1, 1e6), 5e6]}, index=idx)
    monkeypatch.setattr(scanner, "ohlcv", lambda sym, iv: df)
    monkeypatch.setattr(scanner.symbols, "resolve", lambda q: {"symbol": q, "asset_type": "stock"})
    from stockai.data import us_stock

    monkeypatch.setattr(us_stock, "events", lambda t: {"days_to_earnings": 3})
    rows = scanner.scan(["AAA"])
    sig = rows[0]["signals"]
    assert "突破20日高点" in sig and any(s.startswith("放量") for s in sig) and "接近一年新高" in sig
    assert "3天后财报" in sig
    assert rows[0]["direction"] > 0
