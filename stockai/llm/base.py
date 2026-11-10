"""AI 提供方的统一接口。

每个提供方实现一个 Conversation：
- send(text, on_event) -> TurnResult：发一条用户消息，自动跑完"模型调工具 -> 执行 -> 回传"的循环
- 会话历史保存在对象里，可以多轮追问
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import tools as toolbox

EventFn = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnResult:
    text: str
    model: str
    stop_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def noop(_type: str, _data: dict[str, Any]) -> None:
    pass


def api_key(cfg: dict[str, Any]) -> str | None:
    env = cfg.get("api_key_env")
    return os.environ.get(env) if env else cfg.get("api_key")


def run_tools(calls: list[tuple[str, str, dict[str, Any]]], on_event: EventFn,
              log: list[dict[str, Any]]) -> list[tuple[str, str, bool]]:
    """并行执行工具调用。calls: [(call_id, name, args)] -> [(call_id, result_json, is_error)]"""

    def one(c):
        cid, name, args = c
        on_event("tool_call", {"name": name, "args": args})
        t0 = time.time()
        out, err = toolbox.call_tool(name, args)
        ms = int((time.time() - t0) * 1000)
        on_event("tool_result", {"name": name, "ok": not err, "ms": ms, "chars": len(out)})
        log.append({"name": name, "args": args, "ok": not err, "ms": ms})
        return cid, out, err

    if not calls:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(calls))) as ex:
        return list(ex.map(one, calls))


class Conversation:
    max_steps = 25

    def send(self, text: str, on_event: EventFn = noop) -> TurnResult:  # pragma: no cover
        raise NotImplementedError


def make_conversation(provider_name: str, cfg: dict[str, Any], system: str) -> Conversation:
    kind = (cfg.get("type") or "anthropic").lower()
    if kind == "anthropic":
        from .anthropic_provider import AnthropicConversation

        return AnthropicConversation(provider_name, cfg, system)
    if kind in ("openai", "openai_compatible"):
        from .openai_provider import OpenAIConversation

        return OpenAIConversation(provider_name, cfg, system)
    raise ValueError(f"未知的 provider type: {kind}（支持 anthropic / openai）")
