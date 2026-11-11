"""命令行入口。

  stockai analyze NVDA -m short          短线分析
  stockai analyze BTC -m long -p deepseek 用指定 AI 做长线分析
  stockai chat                           多轮对话，AI 按需查数据
  stockai quote NVDA BTC SOL             快速报价（不调用 AI）
  stockai data technicals NVDA           直接查看某个工具的原始输出（调试用，不调用 AI）
  stockai scan                           扫描自选股（不调用 AI），--ai-top 3 对前三名做 AI 分析
  stockai alerts list|add|watch          价位提醒（AI 结论会自动生成提醒）
  stockai review / stats                 回看历史分析 / 统计胜率
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


def _usage_line(turn, cost=None) -> str:
    from .pricing import fmt

    u = turn.usage
    parts = [f"模型 {turn.model}", f"输入 {u.get('input_tokens', 0):,}", f"输出 {u.get('output_tokens', 0):,}"]
    if u.get("cache_read_input_tokens"):
        parts.append(f"缓存命中 {u['cache_read_input_tokens']:,}")
    if u.get("web_search_requests"):
        parts.append(f"网络搜索 {u['web_search_requests']} 次")
    parts.append(f"工具调用 {len(turn.tool_calls)} 次")
    if cost is not None or turn.usage:
        parts.append(fmt(cost))
    return " | ".join(parts)


def _print_verify(v: dict) -> None:
    if not v or not v.get("checked"):
        return
    ratio = v["ratio"] or 0
    color = "green" if ratio >= 0.9 else ("yellow" if ratio >= 0.75 else "red")
    console.print(f"[{color}]数字核对：{v['verified']}/{v['checked']} 个数字能在工具数据中找到（{ratio:.0%}）[/{color}]")
    for u in v.get("unverified", [])[:12]:
        console.print(f"  [yellow]? {u['text']}[/yellow] [dim]…{u['context']}…[/dim]")
    if v.get("unverified"):
        console.print("  [dim]找不到 ≠ 一定是编造（可能是 AI 推算或来自网络搜索），但值得核实[/dim]")


def cmd_analyze(a):
    from .agent import analyze

    res = analyze(a.symbol, a.mode, a.provider, a.question, save=not a.no_save,
                  critic=True if a.critic else None, on_event=_printer(a.verbose))
    console.rule(f"{res.symbol} · {a.mode}")
    console.print(Markdown(res.turn.text or "（没有输出）"))
    if res.critic_notes:
        console.rule("审稿意见")
        console.print(Markdown(res.critic_notes))
    console.rule()
    for w in res.turn.warnings:
        console.print(f"[red]注意：{w}[/red]")
    _print_verify(res.verify)
    console.print(f"[dim]{_usage_line(res.turn, res.cost)}[/dim]")
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
    t = Table("#", "时间(UTC)", "标的", "模式", "当时价", "现价", "至今%", "短线", "入场/止损/目标", "短线结果", "R", "长线", "核对")
    for r in rows:
        s_ = r["summary"] or {}
        st = s_.get("short_term") or {}
        lt = s_.get("long_term") or {}
        try:
            ev = journal.evaluate(r)
        except Exception as e:
            ev = {"note": str(e)[:40]}
        ret = ev.get("return_%")
        color = "green" if (ret or 0) >= 0 else "red"
        plan = f"{st.get('entry')}/{st.get('stop')}/{st.get('targets')}" if st else "-"
        vr = r.get("verify_ratio")
        t.add_row(str(r["id"]), r["created_at"][5:16].replace("T", " "), r["symbol"], r["mode"], str(r["price"] or "-"),
                  str(ev.get("now", "-")), f"[{color}]{ret}[/{color}]" if ret is not None else "-",
                  st.get("bias") or "-", plan, ev.get("short_outcome") or ev.get("note") or "-",
                  str(ev.get("short_r") if ev.get("short_r") is not None else "-"), lt.get("rating") or "-",
                  f"{vr:.0%}" if vr is not None else "-")
    console.print(t)
    console.print("[dim]短线结果：先判断价格是否进入入场区间，再看先触及止损还是目标；超过 horizon 未结束按收盘价计算。"
                  "查看完整报告：stockai show 编号；汇总统计：stockai stats[/dim]")


def cmd_stats(a):
    from . import journal

    with console.status("正在回放历史分析…"):
        st = journal.stats(a.limit)
    t = Table("分组", "分析数", "交易计划", "已入场", "未触发", "已结束", "胜率%", "平均R", "至今平均涨跌%")
    for k, g in st["groups"].items():
        t.add_row(k, *[str(g[c]) if g[c] is not None else "-" for c in
                       ("分析数", "交易计划数", "已入场", "未触发入场", "已结束", "胜率_%", "平均R", "至今平均涨跌_%")])
    console.print(t)
    if st["long_term_by_rating"]:
        t2 = Table("长线评级", "数量", "至今平均涨跌%")
        for k, g in st["long_term_by_rating"].items():
            t2.add_row(k, str(g["数量"]), str(g["至今平均涨跌_%"]))
        console.print(t2)
    console.print(f"[dim]{st['note']}[/dim]")


def cmd_scan(a):
    from . import scanner
    from .config import get_config

    syms = a.symbols or (get_config().get("watchlists") or {}).get(a.watchlist) or []
    if not syms:
        console.print(f"自选列表 '{a.watchlist}' 为空，在 config.yaml 的 watchlists 下配置，或直接传代码")
        return
    with console.status(f"扫描 {len(syms)} 个标的…"):
        rows = scanner.scan(syms)
    t = Table("标的", "价格", "1日%", "1月%", "RSI", "关注度", "方向", "信号")
    for r in rows:
        if "error" in r:
            t.add_row(r["symbol"], "-", "-", "-", "-", "-", "-", f"[red]{r['error'][:50]}[/red]")
            continue
        d = r["direction"]
        dc = "green" if d > 0 else ("red" if d < 0 else "white")
        c1 = "green" if (r["chg_1d_%"] or 0) >= 0 else "red"
        t.add_row(r["symbol"], str(r["price"]), f"[{c1}]{r['chg_1d_%']}[/{c1}]", str(r["chg_1m_%"]), str(r["rsi14"]),
                  str(r["attention"]), f"[{dc}]{d:+d}[/{dc}]", "，".join(r["signals"]) or "-")
    console.print(t)
    console.print("[dim]关注度=信号数量/强度；方向>0 偏多、<0 偏空。纯代码计算，不调用 AI。[/dim]")
    if a.ai_top:
        from .agent import analyze

        top = [r["symbol"] for r in rows if "error" not in r][:a.ai_top]
        for sym in top:
            console.rule(f"AI 分析 {sym}")
            res = analyze(sym, a.mode, a.provider, save=True, on_event=_printer(False))
            console.print(Markdown(res.turn.text or ""))
            _print_verify(res.verify)
            console.print(f"[dim]{_usage_line(res.turn, res.cost)}[/dim]")


def cmd_alerts(a):
    from . import alerts, notify, symbols

    if a.action == "list":
        rows = alerts.list_alerts(active_only=not a.all)
        t = Table("#", "标的", "条件", "价位", "说明", "来源", "状态")
        for r in rows:
            state = "监控中" if r["active"] else f"已触发 {r['triggered_at'][5:16]} @ {r['triggered_price']}"
            t.add_row(str(r["id"]), r["symbol"], "≥" if r["cond"] == "above" else "≤", str(r["price"]),
                      r["note"] or "", r["source"], state)
        console.print(t if rows else "没有提醒")
    elif a.action == "add":
        if not a.symbol or a.cond not in ("above", "below") or a.price is None:
            console.print("用法：stockai alerts add NVDA below 170 --note 回踩支撑")
            return
        s = symbols.resolve(a.symbol)
        i = alerts.add(s["symbol"], s["asset_type"], a.cond, a.price, a.note or "")
        console.print(f"已添加提醒 #{i}")
    elif a.action == "rm":
        alerts.remove(int(a.symbol))
        console.print("已删除")
    elif a.action == "check":
        fired = alerts.check_once()
        console.print(f"触发 {len(fired)} 个" + "".join(f"\n  {f['symbol']} {f['cond']} {f['price']} 现价 {f['triggered_price']} {f['notify']}" for f in fired))
    elif a.action == "watch":
        from .config import get_config

        iv = a.interval or int(get_config().get("alerts", {}).get("interval", 300))
        console.print(f"开始监控（每 {iv} 秒检查一次），Ctrl+C 退出")
        alerts.watch(iv, on_trigger=lambda al, p: console.print(
            f"[bold yellow]触发：{al['symbol']} {'≥' if al['cond'] == 'above' else '≤'} {al['price']}，现价 {p}。{al['note']}[/bold yellow]"))
    elif a.action == "test-notify":
        for r in notify.send("stock-ai 测试", "推送配置正常"):
            console.print(r)


def cmd_bt(a):
    from . import strategies
    from .backtest import evaluate as ev
    from .backtest import report

    if a.action == "list":
        t = Table("策略", "资产组", "参数候选", "说明")
        for name, st in strategies.REGISTRY.items():
            grids = "；".join(f"{g}: {st.group_grid.get(g, st.grid) or '无'}" for g in st.groups)
            t.add_row(name, ",".join(st.groups), grids, st.description)
        console.print(t)
        return
    if a.action == "signal":
        from .backtest import live

        for g in groups_for_signal(a):
            try:
                with console.status(f"计算 {g} 今日信号…"):
                    res = live.signals(a.strategies[0] if a.strategies else None, g)
            except ValueError as e:
                console.print(f"[yellow]{g}：{e}[/yellow]")
                continue
            t = Table("标的", "数据日期", "收盘", "目标仓位", "昨日仓位", "操作", title=f"{g} · {res['strategy']} · 参数 {res['params']}")
            for r in res["rows"]:
                if "error" in r:
                    t.add_row(r["symbol"], "-", "-", "-", "-", f"[red]{r['error'][:40]}[/red]")
                    continue
                c = {"买入": "green", "加仓": "green", "卖出": "red", "减仓": "red"}.get(r["action"], "white")
                t.add_row(r["symbol"], r["date"], str(r["close"]), f"{r['position']:.0%}", f"{r['prev_position']:.0%}",
                          f"[{c}]{r['action']}[/{c}]")
            console.print(t)
            console.print(f"[dim]该组总仓位 {res['portfolio_exposure']:.0%}。{res['note']}。回测表现不代表未来收益。[/dim]")
        return
    if a.action == "board":
        sb = ev.load_scoreboard()
        console.print(f"累计实验：{sb.get('experiments', 0)} 次")
        for g, c in (sb.get("groups") or {}).items():
            console.print(f"[bold]{g}[/bold] 冠军：{c['strategy']} 分数 {c['score']:.3f}（{c['crowned_at']}）{c.get('note', '')}")
        if not sb.get("groups"):
            console.print("还没有任何策略通过护栏成为冠军")
        return
    names = a.strategies or [n for n in strategies.REGISTRY if n != "buy_hold"]
    groups = [a.group] if a.group else ["stocks", "crypto"]
    results = []
    for g in groups:
        for n in names:
            if g not in strategies.get(n).groups:
                continue
            with console.status(f"评测 {n} @ {g}{'（验证集）' if a.action == 'holdout' else ''}…"):
                r = ev.evaluate(n, g, holdout=(a.action == "holdout"))
            results.append(r)
            j = ev.judge(r)
            color = "green" if r["passes"] else "red"
            console.print(f"[{color}]{report.summary_line(r)}[/{color}]")
            if a.action == "eval" and not a.crown:  # --crown 时按顺序判定，结果在下面输出
                console.print(f"  判定：{'✅ 可采纳' if j['accept'] else '❌ 不采纳'}（{j['reason']}）")
            if r.get("causality_detail"):
                console.print(f"  [red]{r['causality_detail']}[/red]")
            if a.verbose:
                console.print("  每年参数：" + "；".join(f"{c['year']}:{c['params']}" for c in r["chosen_params"]))
                for s_, pa in r["per_asset"].items():
                    console.print(f"    {s_:9s} 年化 {report.pct(pa['cagr'])} 回撤 {report.pct(pa['max_dd'])} 交易 {pa['trades']}")
    verdicts = None
    if a.action == "eval" and a.crown:
        # 按顺序判定并加冕：后面的变体要和前面刚加冕的冠军比较
        verdicts = []
        for r in results:
            j = ev.judge(r)
            verdicts.append(j)
            if j["accept"]:
                ev.crown(r, a.change or a.record or "")
                console.print(f"[bold green]{r['strategy']} 成为 {r['group']} 新冠军（{j['reason']}）[/bold green]")
            else:
                console.print(f"{r['strategy']}：不加冕（{j['reason']}）")
    if a.action == "eval" and a.record:
        n = report.record(results, a.record, a.change or "", a.conclusion or "", verdicts)
        console.print(f"已记录为实验 #{n}（optimize/EXPERIMENTS.md）")


def groups_for_signal(a) -> list[str]:
    return [a.group] if a.group else ["stocks", "crypto"]


def cmd_show(a):
    from . import journal

    r = journal.get(a.id)
    if not r:
        console.print("找不到该记录")
        return
    from .pricing import fmt

    console.rule(f"#{r['id']} {r['symbol']} {r['mode']} {r['created_at']} {r['model']}")
    console.print(Markdown(r["report"]))
    if r.get("critic_notes"):
        console.rule("审稿意见")
        console.print(Markdown(r["critic_notes"]))
    _print_verify(r.get("verify") or {})
    console.print(f"[dim]{fmt(r.get('cost') or None)}[/dim]")


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

    from .web.app import app, start_alert_watcher

    if a.alerts:
        start_alert_watcher()
        console.print("价位提醒监控已在后台运行")
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
    s.add_argument("--critic", action="store_true", help="追加一轮审稿：挑错并输出修订版（成本约 +30%%）")
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

    s = sub.add_parser("scan", help="扫描自选股（纯代码打分，不调用 AI）")
    s.add_argument("symbols", nargs="*", help="代码列表；不填则用自选列表")
    s.add_argument("-w", "--watchlist", default="default", help="config.yaml 中 watchlists 的名字")
    s.add_argument("--ai-top", type=int, default=0, help="对排名前 N 个调用 AI 分析")
    s.add_argument("-m", "--mode", choices=["short", "long", "both"], default="short")
    s.add_argument("-p", "--provider")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("alerts", help="价位提醒：list / add / rm / check / watch / test-notify")
    s.add_argument("action", choices=["list", "add", "rm", "check", "watch", "test-notify"])
    s.add_argument("symbol", nargs="?", help="add 时为代码；rm 时为编号")
    s.add_argument("cond", nargs="?", help="above / below")
    s.add_argument("price", nargs="?", type=float)
    s.add_argument("--note")
    s.add_argument("--all", action="store_true", help="list 时包括已触发的")
    s.add_argument("--interval", type=int, help="watch 检查间隔秒数")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("stats", help="按模型/模式统计历史判断表现")
    s.add_argument("-n", "--limit", type=int, default=500)
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("bt", help="策略回测：list / eval / board / signal（今日信号）/ holdout（规则见 GOALS.md）")
    s.add_argument("action", choices=["list", "eval", "board", "holdout", "signal"])
    s.add_argument("strategies", nargs="*", help="策略名；不填则评测全部")
    s.add_argument("-g", "--group", choices=["stocks", "crypto"])
    s.add_argument("--record", help="把本次评测记为一次实验，填写假设描述")
    s.add_argument("--change", help="实验改动说明")
    s.add_argument("--conclusion", help="实验结论")
    s.add_argument("--crown", action="store_true", help="若判定可采纳，则设为该组新冠军")
    s.add_argument("-v", "--verbose", action="store_true", help="显示每年选择的参数和各资产表现")
    s.set_defaults(fn=cmd_bt)

    s = sub.add_parser("show", help="查看某条历史报告")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("providers", help="查看已配置的 AI")
    s.set_defaults(fn=cmd_providers)

    s = sub.add_parser("web", help="启动本地网页界面")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--alerts", action="store_true", help="同时在后台运行价位提醒监控")
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
