"""模拟 Anthropic /v1/messages 与 OpenAI /v1/chat/completions，按脚本返回工具调用。"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FINAL = """## 核心结论
NVDA 现价 178.67，RSI14 为 35.0，市值 4.31 万亿美元。止损 168，目标 190。

```json
{"symbol": "NVDA", "price": 178.67, "short_term": {"bias": "看多", "confidence": 0.5, "entry": [175, 178], "stop": 168, "targets": [190, 200], "horizon": "1-3周", "invalidation": "跌破168"}, "long_term": null, "key_risks": ["测试"]}
```
以上为基于公开数据的研究分析，不构成投资建议。"""

DRAFT = FINAL.replace("RSI14 为 35.0", "RSI14 为 35.0，历史中位 PE 49.5")  # 49.5 是"编造"的
REVISED = "## 审查意见\n- 删除了无法核实的 49.5\n\n## 修订后报告\n" + FINAL


def create_app(reject_optional: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.log = []

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        app.state.log.append({"keys": sorted(body), "beta": req.headers.get("anthropic-beta"),
                              "tools": [t.get("type", "custom") for t in body.get("tools", [])]})
        if reject_optional and ("output_config" in body or "cache_control" in body):
            return JSONResponse(status_code=400, content={"type": "error", "error": {
                "type": "invalid_request_error", "message": "Unknown field: output_config"}})
        msgs = body["messages"]
        last = msgs[-1]
        rounds = sum(1 for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                     and any(c.get("type") == "tool_result" for c in m["content"]))
        if isinstance(last["content"], list) and last["content"] and last["content"][0].get("type") == "tool_result":
            app.state.log.append({"tool_result_ids": [c["tool_use_id"] for c in last["content"]]})
        is_critic = isinstance(last["content"], str) and "审稿人" in last["content"]
        if is_critic:
            content, stop = [{"type": "text", "text": REVISED}], "end_turn"
        elif rounds == 0:
            content = [{"type": "text", "text": "并行拿数据。"},
                       {"type": "tool_use", "id": "tu_1", "name": "get_quote", "input": {"symbol": "NVDA"}},
                       {"type": "tool_use", "id": "tu_2", "name": "get_technicals", "input": {"symbol": "NVDA"}}]
            stop = "tool_use"
        else:
            content, stop = [{"type": "text", "text": DRAFT}], "end_turn"
        return {"id": f"msg_{rounds}", "type": "message", "role": "assistant", "model": body["model"],
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 500,
                          "cache_creation_input_tokens": 0}}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        msgs = body["messages"]
        rounds = sum(1 for m in msgs if m["role"] == "assistant" and m.get("tool_calls"))
        app.state.log.append({"openai_rounds": rounds, "tool_msgs": sum(1 for m in msgs if m["role"] == "tool")})
        if rounds == 0:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_quote", "arguments": json.dumps({"symbol": "NVDA"})}},
                {"id": "c2", "type": "function", "function": {"name": "get_technicals", "arguments": "{bad json"}},
            ]}
            finish = "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": FINAL}, "stop"
        return {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550}}

    return app
