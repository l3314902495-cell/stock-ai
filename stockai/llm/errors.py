"""把 SDK 异常翻译成可操作的中文提示（具体异常优先）。"""
from __future__ import annotations

import anthropic
import openai


def explain(e: Exception) -> str:
    if isinstance(e, (anthropic.AuthenticationError, openai.AuthenticationError)):
        return "API Key 无效或未设置。检查 .env 里对应的变量，以及 config.yaml 的 api_key_env。"
    if isinstance(e, (anthropic.PermissionDeniedError, openai.PermissionDeniedError)):
        return f"没有权限调用该模型/接口：{e}"
    if isinstance(e, (anthropic.NotFoundError, openai.NotFoundError)):
        return f"模型名或接口地址不存在（检查 model 和 base_url）：{e}"
    if isinstance(e, (anthropic.RateLimitError, openai.RateLimitError)):
        return "触发限流或额度不足，稍后再试，或换一个 provider（-p）。"
    if isinstance(e, (anthropic.BadRequestError, openai.BadRequestError)):
        return f"请求被拒绝（参数不兼容？中转站可能不支持某些参数）：{e}"
    if isinstance(e, (anthropic.APIStatusError, openai.APIStatusError)):
        return f"AI 接口返回错误 {e.status_code}：{e}"
    if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError)):
        return "连不上 AI 接口。检查网络、base_url，需要代理的话在 provider 里配置 proxy。"
    return f"{type(e).__name__}: {e}"
