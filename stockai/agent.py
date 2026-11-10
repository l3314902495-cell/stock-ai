"""分析流程：组装提示词 -> 调 AI（自动调用工具）-> [可选审稿] -> 数字核对 -> 估算成本 -> 保存。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import journal, pricing, prompts, symbols, verify
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
    return name, make_conversation(name, cfg, prompts.SYSTEM_PROMPT)


def _merge_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


@dataclass
class AnalysisResult:
    symbol: str
    mode: str
    provider: str
    turn: TurnResult
    report_path: Path | None
    journal_id: int | None
    verify: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] | None = None
    critic_notes: str | None = None


def analyze(query: str, mode: str = "both", provider: str | None = None, question: str | None = None,
            save: bool = True, critic: bool | None = None, on_event: EventFn = noop) -> AnalysisResult:
    if mode not in prompts.MODE_TEXT:
        raise ValueError("mode 只能是 short / long / both")
    sym = symbols.resolve(query)
    label = f"{sym['symbol']}（{sym.get('name') or ''}，{'加密货币' if sym['asset_type'] == 'crypto' else '美股/ETF'}）"
    on_event("status", {"text": f"已识别标的：{label}"})
    name, cfg = provider_cfg(provider)
    conv = make_conversation(name, cfg, prompts.SYSTEM_PROMPT)
    turn = conv.send(prompts.user_message(label, mode, now_str(), question), on_event)

    tool_log = list(turn.tool_calls)
    extra = list(turn.extra_texts)
    outputs = lambda: [t["output"] for t in tool_log if t.get("output")]  # noqa: E731
    check = verify.check(turn.text, outputs(), extra)

    # 审稿：同一会话里追加一轮（复用已有数据和缓存），输出修订版
    critic_notes = None
    if critic is None:
        critic = bool(get_config().get("analysis", {}).get("critic", False))
    if critic and turn.text and turn.stop_reason not in ("refusal",):
        on_event("status", {"text": f"审稿中…（数字核对：{check['verified']}/{check['checked']} 可在数据中找到）"})
        rv = conv.send(prompts.critic_message(check["unverified"]), on_event)
        tool_log += rv.tool_calls
        extra += rv.extra_texts
        if "## 修订后报告" in rv.text:
            notes, revised = rv.text.split("## 修订后报告", 1)
            critic_notes = notes.replace("## 审查意见", "").strip()
            revised = revised.strip()
        else:
            critic_notes, revised = rv.text.strip(), ""
        if revised and journal.extract_json(revised):
            turn = TurnResult(text=revised, model=rv.model, stop_reason=rv.stop_reason, tool_calls=tool_log,
                              usage=_merge_usage(turn.usage, rv.usage), warnings=turn.warnings + rv.warnings,
                              extra_texts=extra)
        else:
            turn.usage = _merge_usage(turn.usage, rv.usage)
            turn.warnings.append("审稿轮没有输出完整的修订版报告，保留原报告")
        check = verify.check(turn.text, outputs(), extra)

    turn.tool_calls = tool_log
    cost = pricing.estimate(turn.usage, turn.model, cfg)

    path, jid = None, None
    if save and turn.text:
        out_dir = Path(get_config().get("reports_dir") or "./reports").expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now():%Y%m%d_%H%M}_{sym['symbol']}_{mode}"
        path = out_dir / f"{stem}.md"
        header = (f"<!-- {sym['symbol']} | {mode} | {name} | {turn.model} | {now_str()} | "
                  f"tokens in={turn.usage.get('input_tokens', 0)} out={turn.usage.get('output_tokens', 0)} | "
                  f"{pricing.fmt(cost)} | 数字核对 {check['verified']}/{check['checked']} -->\n\n")
        body = turn.text + (f"\n\n---\n<details><summary>审稿意见</summary>\n\n{critic_notes}\n</details>\n"
                            if critic_notes else "")
        path.write_text(header + body, encoding="utf-8")
        # 工具原始数据单独存一份，方便核对与复现
        (out_dir / f"{stem}.tools.json").write_text(
            json.dumps({"tool_calls": tool_log, "web_citations": extra, "verify": check}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        jid = journal.save(sym["symbol"], sym["asset_type"], mode, name, turn.model, turn.text, turn.usage,
                           tool_log=tool_log, verify=check, cost=cost, critic_notes=critic_notes)
        summary = journal.extract_json(turn.text) or {}
        if get_config().get("alerts", {}).get("auto_from_analysis", True) and summary:
            from . import alerts

            try:
                n = alerts.from_analysis(jid, sym["symbol"], sym["asset_type"], summary, summary.get("price"))
                if n:
                    on_event("status", {"text": f"已根据结论创建 {len(n)} 个价位提醒（stockai alerts list 查看）"})
            except Exception:
                pass
    return AnalysisResult(sym["symbol"], mode, name, turn, path, jid, check, cost, critic_notes)
