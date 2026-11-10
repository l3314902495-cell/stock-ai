"""消息推送：Telegram / Server酱 / Bark / ntfy / 通用 webhook。在 config.yaml 的 notify 下配置，可同时开多个。

notify:
  telegram:   {bot_token_env: TG_BOT_TOKEN, chat_id_env: TG_CHAT_ID}
  serverchan: {key_env: SERVERCHAN_KEY}           # 微信推送 https://sct.ftqq.com
  bark:       {key_env: BARK_KEY, server: https://api.day.app}   # iOS
  ntfy:       {topic_env: NTFY_TOPIC, server: https://ntfy.sh}
  webhook:    {url_env: NOTIFY_WEBHOOK_URL}       # POST JSON {"title","body"}
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import requests

from .config import data_proxy, get_config


def _env(cfg: dict, key: str) -> str | None:
    name = cfg.get(key)
    return os.environ.get(name) if name else None


def _post(url: str, **kw) -> None:
    proxy = data_proxy()
    r = requests.request(kw.pop("method", "POST"), url, timeout=15, proxies={"https": proxy} if proxy else None, **kw)
    r.raise_for_status()


def send(title: str, body: str) -> list[str]:
    """推送到所有已配置的渠道，返回结果说明列表。没有配置任何渠道时只打印。"""
    cfg: dict[str, Any] = get_config().get("notify") or {}
    results = []
    for name, c in cfg.items():
        c = c or {}
        try:
            if name == "telegram":
                tok, chat = _env(c, "bot_token_env"), _env(c, "chat_id_env")
                if not tok or not chat:
                    raise ValueError("缺少 bot token 或 chat id")
                _post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": f"{title}\n\n{body}"})
            elif name == "serverchan":
                key = _env(c, "key_env")
                if not key:
                    raise ValueError("缺少 key")
                _post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title[:32], "desp": body})
            elif name == "bark":
                key = _env(c, "key_env")
                if not key:
                    raise ValueError("缺少 key")
                server = (c.get("server") or "https://api.day.app").rstrip("/")
                _post(f"{server}/{key}/{quote(title, safe='')}/{quote(body, safe='')}", method="GET")
            elif name == "ntfy":
                topic = _env(c, "topic_env")
                if not topic:
                    raise ValueError("缺少 topic")
                server = (c.get("server") or "https://ntfy.sh").rstrip("/")
                _post(f"{server}/{topic}", data=body.encode(), headers={"Title": title.encode("utf-8")})
            elif name == "webhook":
                url = _env(c, "url_env")
                if not url:
                    raise ValueError("缺少 url")
                _post(url, json={"title": title, "body": body, "text": f"{title}\n{body}"})
            else:
                raise ValueError("未知渠道")
            results.append(f"{name}: 已发送")
        except Exception as e:
            results.append(f"{name}: 失败 {str(e)[:100]}")
    if not cfg:
        results.append("未配置推送渠道（只在终端显示）")
    return results
