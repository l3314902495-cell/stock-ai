"""分析流程：组装提示词 -> 调 AI（自动调用工具）-> 保存报告和日志。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import journal, prompts, symbols
from .config import get_config
from .llm.base import Conversation, EventFn, TurnResult, make_conversation, noop
from .util import now_str


def provider_cfg(name: str | None) -> tuple[str, dict[str, Any]]:
    llm = get_config()["llm"]
    name = name or llm.get("default")
    providers = llm.get("providers") or {}
    if name not in providers:
        raise ValueError(f"配置里没有 provider '{name}'，可选：{', '.join(providers)}")
    return name, providers[name]


def new_conversation(provider: str | None = None) -> tuple[str, Conversation]:
    name, cfg = provider_cfg(provider)
    system = prompts.SYSTEM_PROMPT
    return name, make_conversation(name, cfg, system)


@dataclass
class AnalysisResult:
    symbol: str
    mode: str
    provider: str
    turn: TurnResult
    report_path: Path | None
    journal_id: int | None


def analyze(query: str, mode: str = "both", provider: str | None = None, question: str | None = None,
            save: bool = True, on_event: EventFn = noop) -> AnalysisResult:
    if mode not in prompts.MODE_TEXT:
        raise ValueError("mode 只能是 short / long / both")
    sym = symbols.resolve(query)
    label = f"{sym['symbol']}（{sym.get('name') or ''}，{'加密货币' if sym['asset_type'] == 'crypto' else '美股/ETF'}）"
    on_event("status", {"text": f"已识别标的：{label}"})
    name, conv = new_conversation(provider)
    msg = prompts.user_message(label, mode, now_str(), question)
    turn = conv.send(msg, on_event)

    path, jid = None, None
    if save and turn.text:
        out_dir = Path(get_config().get("reports_dir") or "./reports").expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{datetime.now():%Y%m%d_%H%M}_{sym['symbol']}_{mode}.md"
        header = (f"<!-- {sym['symbol']} | {mode} | {name} | {turn.model} | {now_str()} | "
                  f"tokens in={turn.usage.get('input_tokens', 0)} out={turn.usage.get('output_tokens', 0)} -->\n\n")
        path.write_text(header + turn.text, encoding="utf-8")
        jid = journal.save(sym["symbol"], sym["asset_type"], mode, name, turn.model, turn.text, turn.usage)
    return AnalysisResult(sym["symbol"], mode, name, turn, path, jid)
