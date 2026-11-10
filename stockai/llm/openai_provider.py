"""OpenAI 兼容格式：DeepSeek、通义千问、Kimi、智谱、各类中转站、本地 Ollama 等。

要求模型支持 function calling（tools）。
"""
from __future__ import annotations

import json
from typing import Any

import openai

from .. import tools as toolbox
from .base import Conversation, EventFn, TurnResult, api_key, noop, run_tools


class OpenAIConversation(Conversation):
    def __init__(self, name: str, cfg: dict[str, Any], system: str):
        self.name = name
        self.cfg = cfg
        self.model = cfg["model"]
        kwargs: dict[str, Any] = {"max_retries": int(cfg.get("max_retries", 3)), "timeout": float(cfg.get("timeout", 600)),
                                  "api_key": api_key(cfg) or "not-set"}
        if cfg.get("base_url"):
            kwargs["base_url"] = cfg["base_url"]
        if cfg.get("proxy"):
            kwargs["http_client"] = openai.DefaultHttpxClient(proxy=cfg["proxy"])
        self.client = openai.OpenAI(**kwargs)
        self.tools = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}}
            for t in toolbox.tool_specs()
        ]
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def send(self, text: str, on_event: EventFn = noop) -> TurnResult:
        self.messages.append({"role": "user", "content": text})
        usage: dict[str, int] = {}
        log: list[dict[str, Any]] = []
        warnings: list[str] = []
        model_name = self.model
        for step in range(self.max_steps):
            on_event("status", {"text": f"{self.name} 思考中…（第 {step + 1} 轮）"})
            params: dict[str, Any] = {"model": self.model, "messages": self.messages, "tools": self.tools}
            if self.cfg.get("max_tokens"):
                params["max_tokens"] = int(self.cfg["max_tokens"])
            if self.cfg.get("extra_body"):
                params["extra_body"] = self.cfg["extra_body"]
            resp = self.client.chat.completions.create(**params)
            model_name = resp.model or self.model
            if resp.usage:
                usage["input_tokens"] = usage.get("input_tokens", 0) + (resp.usage.prompt_tokens or 0)
                usage["output_tokens"] = usage.get("output_tokens", 0) + (resp.usage.completion_tokens or 0)
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = msg.tool_calls or []
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if tool_calls:
                assistant["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in tool_calls
                ]
            self.messages.append(assistant)

            if tool_calls:
                if msg.content and msg.content.strip():
                    on_event("thought", {"text": msg.content.strip()})
                calls, bad = [], []
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("参数必须是 JSON 对象")
                        calls.append((tc.id, tc.function.name, args))
                    except Exception as e:
                        bad.append((tc.id, json.dumps({"error": f"工具参数不是合法 JSON: {e}"}, ensure_ascii=False), True))
                for cid, out, _err in bad + run_tools(calls, on_event, log):
                    self.messages.append({"role": "tool", "tool_call_id": cid, "content": out})
                continue

            if choice.finish_reason == "length":
                warnings.append("输出达到长度上限被截断，可在配置中调大 max_tokens")
            return TurnResult(text=(msg.content or "").strip(), model=model_name, stop_reason=choice.finish_reason or "stop",
                              tool_calls=log, usage=usage, warnings=warnings)

        warnings.append(f"工具调用超过 {self.max_steps} 轮，已停止")
        return TurnResult(text="", model=model_name, stop_reason="max_steps", tool_calls=log, usage=usage,
                          warnings=warnings)
