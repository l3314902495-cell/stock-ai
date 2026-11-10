"""Anthropic 格式（Claude 官方 API 或兼容 Anthropic 格式的中转站）。

官方 API 默认开启：
- 提示缓存（系统提示 + 工具定义 + 历史对话，多轮工具调用时能省大量输入 token）
- effort 档位（output_config.effort）
- 服务端拒答回退（fallbacks="default"，被安全策略拒答时自动换模型重试）
中转站可能不支持这些参数：遇到 400 会自动去掉可选参数重试一次，也可在配置里关闭。
"""
from __future__ import annotations

from typing import Any

import anthropic

from .. import tools as toolbox
from .base import Conversation, EventFn, TurnResult, api_key, noop, run_tools

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicConversation(Conversation):
    def __init__(self, name: str, cfg: dict[str, Any], system: str):
        self.name = name
        self.cfg = cfg
        self.system = system
        self.model = cfg.get("model") or "claude-opus-5-5"
        self.max_tokens = int(cfg.get("max_tokens") or 16000)
        official = not cfg.get("base_url")
        self.use_cache = cfg.get("prompt_cache", True)
        self.effort = cfg.get("effort", "high")
        sf = cfg.get("server_fallback", "auto")
        self.use_fallback = official if sf == "auto" else bool(sf)
        kwargs: dict[str, Any] = {"max_retries": int(cfg.get("max_retries", 3)), "timeout": float(cfg.get("timeout", 600))}
        key = api_key(cfg)
        if key:
            kwargs["api_key"] = key
        if cfg.get("base_url"):
            kwargs["base_url"] = cfg["base_url"]
        if cfg.get("proxy"):
            kwargs["http_client"] = anthropic.DefaultHttpxClient(proxy=cfg["proxy"])
        self.client = anthropic.Anthropic(**kwargs)
        self.tools = toolbox.tool_specs()
        self.messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def _create(self, warnings: list[str]):
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "tools": self.tools,
            "messages": self.messages,
        }
        optional: dict[str, Any] = {}
        if self.use_cache:
            optional["cache_control"] = {"type": "ephemeral"}
        if self.effort:
            optional["output_config"] = {"effort": self.effort}
        if self.cfg.get("extra_body"):
            params["extra_body"] = self.cfg["extra_body"]

        def call(with_optional: bool):
            p = {**params, **(optional if with_optional else {})}
            if self.use_fallback and with_optional:
                return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **p)
            return self.client.messages.create(**p)

        try:
            return call(True)
        except anthropic.BadRequestError as e:
            if not optional and not self.use_fallback:
                raise
            warnings.append(f"接口不接受可选参数（{str(e)[:120]}），已去掉 cache/effort/fallback 重试。"
                            "可在配置中设置 prompt_cache: false, effort: null, server_fallback: false")
            self.use_cache, self.effort, self.use_fallback = False, None, False
            return call(False)

    def send(self, text: str, on_event: EventFn = noop) -> TurnResult:
        self.messages.append({"role": "user", "content": text})
        usage: dict[str, int] = {}
        log: list[dict[str, Any]] = []
        warnings: list[str] = []
        resp = None
        for step in range(self.max_steps):
            on_event("status", {"text": f"{self.name} 思考中…（第 {step + 1} 轮）"})
            resp = self._create(warnings)
            u = resp.usage
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage[k] = usage.get(k, 0) + (getattr(u, k, 0) or 0)

            # 原样回传 assistant 内容（含 thinking 块），保证多轮工具调用有效
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "refusal":
                d = getattr(resp, "stop_details", None)
                why = getattr(d, "explanation", None) if d else None
                return TurnResult(text=f"模型拒绝回答。{why or ''}", model=resp.model, stop_reason="refusal",
                                  tool_calls=log, usage=usage, warnings=warnings)
            if resp.stop_reason == "pause_turn":
                continue

            calls = [(b.id, b.name, b.input or {}) for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "tool_use" and calls:
                for b in resp.content:
                    if b.type == "text" and b.text.strip():
                        on_event("thought", {"text": b.text.strip()})
                results = run_tools(calls, on_event, log)
                # 所有工具结果放在同一条 user 消息里
                self.messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": cid, "content": out, **({"is_error": True} if err else {})}
                    for cid, out, err in results
                ]})
                continue

            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "max_tokens":
                warnings.append("输出达到 max_tokens 上限被截断，可在配置中调大 max_tokens")
            return TurnResult(text=text, model=resp.model, stop_reason=resp.stop_reason, tool_calls=log,
                              usage=usage, warnings=warnings)

        text = "".join(b.text for b in (resp.content if resp else []) if b.type == "text")
        warnings.append(f"工具调用超过 {self.max_steps} 轮，已停止")
        return TurnResult(text=text, model=self.model, stop_reason="max_steps", tool_calls=log, usage=usage,
                          warnings=warnings)
