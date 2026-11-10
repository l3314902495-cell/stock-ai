"""用本地模拟 AI 服务测试完整分析流程（离线，不访问任何真实 API 或数据源）。"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn

from tests.mock_llm import create_app

FAKE_TOOL = {
    "get_quote": {"quote": {"price": 178.67, "market_cap": "4.31T"}},
    "get_technicals": {"timeframes": {"1d": {"rsi14": 35.04}}},
}


def _serve(app) -> tuple[uvicorn.Server, int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, port


@pytest.fixture(scope="module")
def env(tmp_path_factory, monkeypatch_module):
    tmp = tmp_path_factory.mktemp("stockai")
    normal, strict = create_app(), create_app(reject_optional=True)
    s1, p1 = _serve(normal)
    s2, p2 = _serve(strict)
    cfg = {
        "llm": {"default": "a", "providers": {
            "a": {"type": "anthropic", "model": "claude-opus-5-5", "api_key_env": "MOCK_KEY", "base_url": f"http://127.0.0.1:{p1}"},
            "official_like": {"type": "anthropic", "model": "claude-opus-5-5", "api_key_env": "MOCK_KEY",
                              "base_url": f"http://127.0.0.1:{p1}", "server_fallback": True, "web_search": True},
            "strict": {"type": "anthropic", "model": "claude-opus-5-5", "api_key_env": "MOCK_KEY", "base_url": f"http://127.0.0.1:{p2}",
                       "web_search": True},
            "oa": {"type": "openai", "model": "mock-gpt", "api_key_env": "MOCK_KEY", "base_url": f"http://127.0.0.1:{p1}/v1",
                   "price": {"input": 1, "output": 2, "currency": "CNY"}},
        }},
        "journal": {"path": str(tmp / "j.db")}, "cache": {"path": str(tmp / "c.db")}, "reports_dir": str(tmp / "reports"),
    }
    (tmp / "config.yaml").write_text(json.dumps(cfg))
    monkeypatch_module.setenv("MOCK_KEY", "test")
    monkeypatch_module.setenv("NO_PROXY", "127.0.0.1,localhost")

    from stockai import config, symbols, tools

    config.load_config(str(tmp / "config.yaml"))
    monkeypatch_module.setattr(symbols, "resolve", lambda q: {"symbol": "NVDA", "asset_type": "stock", "name": "NVIDIA"})

    def fake_call(name, args):
        if name in FAKE_TOOL:
            return json.dumps(FAKE_TOOL[name]), False
        return json.dumps({"error": "unknown"}), True

    monkeypatch_module.setattr(tools, "call_tool", fake_call)
    yield {"normal": normal, "strict": strict, "tmp": tmp}
    s1.should_exit = s2.should_exit = True


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_anthropic_flow_and_verify(env):
    from stockai import journal
    from stockai.agent import analyze

    env["normal"].state.log.clear()
    res = analyze("NVDA", "short", "a", critic=False)
    assert "核心结论" in res.turn.text
    # 两个工具结果放在同一条消息里返回
    ids = [l["tool_result_ids"] for l in env["normal"].state.log if "tool_result_ids" in l]
    assert ids and sorted(ids[0]) == ["tu_1", "tu_2"]
    # 中转站（有 base_url）默认不开 fallback / web_search
    assert all(l.get("beta") is None for l in env["normal"].state.log if "keys" in l)
    # 数字核对：49.5 不在工具数据里
    assert any(u["text"] == "49.5" for u in res.verify["unverified"])
    assert res.cost is None  # 中转站未配置价格
    saved = journal.get(res.journal_id, with_tools=True)
    assert len(saved["tool_log"]) == 2 and saved["tool_log"][0]["output"]
    assert res.report_path.exists() and res.report_path.with_suffix("").with_suffix(".tools.json").exists()


def test_critic_replaces_report(env):
    from stockai.agent import analyze

    res = analyze("NVDA", "short", "a", critic=True)
    assert "49.5" not in res.turn.text
    assert res.critic_notes and "49.5" in res.critic_notes
    assert not any(u["text"] == "49.5" for u in res.verify["unverified"])
    assert res.turn.usage["input_tokens"] == 3000  # 三次请求合计


def test_official_like_sends_fallback_and_web_search(env):
    from stockai.agent import analyze

    env["normal"].state.log.clear()
    analyze("NVDA", "short", "official_like", critic=False, save=False)
    reqs = [l for l in env["normal"].state.log if "keys" in l]
    assert all(l["beta"] == "server-side-fallback-2026-07-01" for l in reqs)
    assert all("web_search_20260209" in l["tools"] for l in reqs)
    assert all("output_config" in l["keys"] and "cache_control" in l["keys"] for l in reqs)


def test_strict_relay_drops_optional_params(env):
    from stockai.agent import analyze

    res = analyze("NVDA", "short", "strict", critic=False, save=False)
    assert "核心结论" in res.turn.text
    assert any("可选参数" in w for w in res.turn.warnings)
    later = [l for l in env["strict"].state.log if "keys" in l][1:]
    assert later and all("output_config" not in l["keys"] and "web_search_20260209" not in l["tools"] for l in later)


def test_openai_flow_bad_json_and_price(env):
    from stockai.agent import analyze

    env["normal"].state.log.clear()
    res = analyze("NVDA", "short", "oa", critic=False, save=False)
    assert "核心结论" in res.turn.text
    second = [l for l in env["normal"].state.log if l.get("openai_rounds") == 1][0]
    assert second["tool_msgs"] == 2  # 正常结果 + 参数错误的结果都要回传
    assert res.cost == {"amount": round((1000 * 1 + 100 * 2) / 1e6, 4), "currency": "CNY"}
