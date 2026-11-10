"""命令行入口。

  stockai analyze NVDA -m short          短线分析
  stockai analyze BTC -m long -p deepseek 用指定 AI 做长线分析
  stockai chat                           多轮对话，AI 按需查数据
  stockai quote NVDA BTC SOL             快速报价（不调用 AI）
  stockai data technicals NVDA           直接查看某个工具的原始输出（调试用，不调用 AI）
  stockai review                         回看历史分析的结果
  stockai providers                      查看已配置的 AI
  stockai web                            启动本地网页
"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import load_config

console = Console()


def _printer(verbose: bool):
    def on_event(kind: str, data: dict):
        if kind == "tool_call":
            args = json.dumps(data["args"], ensure_ascii=False)
            console.print(f"  [cyan]→ {data['name']}[/cyan] [dim]{args[:120]}[/dim]")
        elif kind == "tool_result":
            mark = "[green]✓[/green]" if data["ok"] else "[red]✗[/red]"
            console.print(f"    {mark} [dim]{data['name']} {data['ms']}ms {data['chars']} 字符[/dim]")
        elif kind == "status":
            console.print(f"[yellow]{data['text']}[/yellow]")
        elif kind == "thought" and verbose:
            console.print(f"[dim italic]{data['text'][:300]}[/dim italic]")

    return on_event


def _usage_line(turn) -> str:
    u = turn.usage
    parts = [f"模型 {turn.model}", f"输入 {u.get('input_tokens', 0):,}", f"输出 {u.get('output_tokens', 0):,}"]
    if u.get("cache_read_input_tokens"):
        parts.append(f"缓存命中 {u['cache_read_input_tokens']:,}")
    parts.append(f"工具调用 {len(turn.tool_calls)} 次")
    return " | ".join(parts)


def cmd_analyze(a):
    from .agent import analyze

    res = analyze(a.symbol, a.mode, a.provider, a.question, save=not a.no_save, on_event=_printer(a.verbose))
    console.rule(f"{res.symbol} · {a.mode}")
    console.print(Markdown(res.turn.text or "（没有输出）"))
    console.rule()
    for w in res.turn.warnings:
        console.print(f"[red]注意：{w}[/red]")
    console.print(f"[dim]{_usage_line(res.turn)}[/dim]")
    if res.report_path:
        console.print(f"[dim]报告已保存：{res.report_path}（日志 #{res.journal_id}）[/dim]")


def cmd_chat(a):
    from .agent import new_conversation
    from .prompts import CHAT_HINT
    from .util import now_str

    name, conv = new_conversation(a.provider)
    console.print(f"[bold]对话模式[/bold]（{name}）。输入问题，例如：'NVDA 财报前适合买吗' / 'BTC 资金费率说明什么'；输入 exit 退出。")
    first = True
    while True:
        try:
            q = console.input("[bold green]你> [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "q"):
            break
        msg = f"当前时间：{now_str()}\n{CHAT_HINT}\n\n{q}" if first else f"（当前时间：{now_str()}）{q}"
        first = False
        turn = conv.send(msg, _printer(a.verbose))
        console.print(Markdown(turn.text or "（没有输出）"))
        for w in turn.warnings:
            console.print(f"[red]注意：{w}[/red]")
        console.print(f"[dim]{_usage_line(turn)}[/dim]")


def cmd_quote(a):
    from . import tools

    t = Table("代码", "类型", "价格", "涨跌%", "52周/24h区间", "市值", "备注")
    for s in a.symbols:
        out, err = tools.call_tool("get_quote", {"symbol": s})
        d = json.loads(out)
        if err:
            t.add_row(s, "-", "-", "-", "-", "-", d.get("error", "")[:60])
            continue
        q, asset = d["quote"], d["asset"]
        chg = q.get("change_pct", q.get("change_24h_pct"))
        rng = q.get("52w_range") or [q.get("low_24h"), q.get("high_24h")]
        color = "green" if (chg or 0) >= 0 else "red"
        t.add_row(asset["symbol"], asset["asset_type"], str(q.get("price")), f"[{color}]{chg}[/{color}]",
                  f"{rng[0]} - {rng[1]}", str(q.get("market_cap", "-")), str(q.get("name") or q.get("exchange") or ""))
    console.print(t)


def cmd_data(a):
    from . import tools

    name = a.tool if a.tool.startswith(("get_", "run_", "resolve_", "trade_")) else f"get_{a.tool}"
    args: dict = {}
    if name == "get_market_context":
        args = {"asset_type": a.symbol or "stock"}
    elif name == "resolve_symbol":
        args = {"query": a.symbol}
    else:
        args = {"symbol": a.symbol}
    if a.args:
        args.update(json.loads(a.args))
    out, err = tools.call_tool(name, args)
    console.print_json(out)
    if err:
        sys.exit(1)


def cmd_review(a):
    from . import journal

    rows = journal.list_recent(a.limit)
    if not rows:
        console.print("还没有分析记录。先运行 stockai analyze XXX")
        return
    t = Table("#", "时间(UTC)", "标的", "模式", "当时价", "现价", "至今%", "短线判断", "止损/目标", "短线结果", "长线评级")
    for r in rows:
        s = r["summary"] or {}
        st = s.get("short_term") or {}
        lt = s.get("long_term") or {}
        try:
            ev = journal.evaluate(r)
        except Exception as e:
            ev = {"note": str(e)[:40]}
        ret = ev.get("return_%")
        color = "green" if (ret or 0) >= 0 else "red"
        t.add_row(str(r["id"]), r["created_at"][:16].replace("T", " "), r["symbol"], r["mode"], str(r["price"] or "-"),
                  str(ev.get("now", "-")), f"[{color}]{ret}[/{color}]" if ret is not None else "-",
                  st.get("bias") or "-", f"{st.get('stop')}/{st.get('targets')}" if st else "-",
                  ev.get("short_outcome") or ev.get("note") or "-", lt.get("rating") or "-")
    console.print(t)
    console.print("[dim]短线结果按分析之后的 1 小时 K 线判断先触及止损还是目标 1。查看完整报告：stockai show 编号</dim>")


def cmd_show(a):
    from . import journal

    r = journal.get(a.id)
    if not r:
        console.print("找不到该记录")
        return
    console.rule(f"#{r['id']} {r['symbol']} {r['mode']} {r['created_at']} {r['model']}")
    console.print(Markdown(r["report"]))


def cmd_providers(a):
    from .config import get_config
    from .llm.base import api_key

    llm = get_config()["llm"]
    t = Table("名称", "类型", "模型", "base_url", "API Key", "默认")
    for name, cfg in (llm.get("providers") or {}).items():
        has = "✓" if api_key(cfg) else f"[red]未设置 {cfg.get('api_key_env')}[/red]"
        t.add_row(name, cfg.get("type", "anthropic"), cfg.get("model", "-"), cfg.get("base_url") or "官方",
                  has, "★" if name == llm.get("default") else "")
    console.print(t)


def cmd_web(a):
    import uvicorn

    from .web.app import app

    console.print(f"打开浏览器访问 http://{a.host}:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


def cmd_cache_clear(a):
    from . import cache

    cache.clear()
    console.print("缓存已清空")


def main(argv=None):
    p = argparse.ArgumentParser(prog="stockai", description="AI 美股/加密货币分析工具")
    p.add_argument("--config", help="配置文件路径（默认 ./config.yaml 或 ~/.stock-ai/config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("analyze", help="AI 分析一个标的")
    s.add_argument("symbol")
    s.add_argument("-m", "--mode", choices=["short", "long", "both"], default="both")
    s.add_argument("-p", "--provider", help="使用哪个 AI（config.yaml 中 providers 的名字）")
    s.add_argument("-q", "--question", help="额外关心的问题")
    s.add_argument("--no-save", action="store_true", help="不保存报告和日志")
    s.add_argument("-v", "--verbose", action="store_true", help="显示 AI 的中间说明")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("chat", help="多轮对话")
    s.add_argument("-p", "--provider")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("quote", help="快速报价（不调用 AI）")
    s.add_argument("symbols", nargs="+")
    s.set_defaults(fn=cmd_quote)

    s = sub.add_parser("data", help="查看工具原始输出（不调用 AI）")
    s.add_argument("tool", help="technicals / fundamentals / long_term_financials / derivatives / news / events / "
                                "peers / market_context / run_dcf / resolve_symbol")
    s.add_argument("symbol", nargs="?", default="")
    s.add_argument("--args", help='额外参数 JSON，如 \'{"timeframes":["15m","1h"]}\'')
    s.set_defaults(fn=cmd_data)

    s = sub.add_parser("review", help="回看历史分析结果")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("show", help="查看某条历史报告")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("providers", help="查看已配置的 AI")
    s.set_defaults(fn=cmd_providers)

    s = sub.add_parser("web", help="启动本地网页界面")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("cache-clear", help="清空数据缓存")
    s.set_defaults(fn=cmd_cache_clear)

    a = p.parse_args(argv)
    load_config(a.config)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        console.print("\n已中断")
    except Exception as e:  # 给出友好的错误
        from .llm.errors import explain

        console.print(f"[red]出错：{explain(e)}[/red]")
        if "-v" in sys.argv or "--verbose" in sys.argv:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()
