"""评测结果格式化 + 实验记录（追加到 optimize/EXPERIMENTS.md）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import data
from .evaluate import bump_experiments, judge

EXPERIMENTS = data.ROOT / "optimize" / "EXPERIMENTS.md"

_GUARD_CN = {"not_benchmark": "非基准", "causal": "无未来数据", "min_trades": "交易≥100笔",
             "max_dd": "回撤≤基准60%", "min_cagr": "年化≥基准60%"}


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def summary_line(r: dict[str, Any]) -> str:
    m, b = r["metrics"], r["benchmark"]
    fails = [_GUARD_CN.get(k, k) for k, v in r["guardrails"].items() if not v]
    score = "-" if r["score"] is None else f"{r['score']:.3f}"
    return (f"{r['strategy']} @ {r['group']}：分数 {score}｜年化 {pct(m['cagr'])} 回撤 {pct(m['max_dd'])} "
            f"Calmar {m['calmar']:.3f} 夏普 {m['sharpe']:.2f} 交易 {m['trades']}｜基准 年化 {pct(b['cagr'])} "
            f"回撤 {pct(b['max_dd'])} Calmar {b['calmar']:.3f}｜"
            + ("护栏全部通过" if not fails else "未通过：" + "、".join(fails)))


def record(results: list[dict[str, Any]], hypothesis: str, change: str = "", conclusion: str = "",
           verdicts: list[dict[str, Any]] | None = None) -> int:
    """把一次实验（一个假设，可能涉及多个资产组）写入实验记录，返回累计实验编号。
    verdicts：与 results 一一对应的判定（按顺序加冕时，后面的变体要和前面刚加冕的比较）。"""
    n = bump_experiments()
    EXPERIMENTS.parent.mkdir(parents=True, exist_ok=True)
    if not EXPERIMENTS.exists():
        EXPERIMENTS.write_text("# 实验记录\n\n每轮一个假设，成功和失败都记录。由 `stockai bt eval ... --record` 自动追加。\n\n")
    lines = [f"## #{n} {datetime.now(timezone.utc):%Y-%m-%d}：{hypothesis}", ""]
    if change:
        lines += [f"- 改动：{change}"]
    for i, r in enumerate(results):
        j = verdicts[i] if verdicts else judge(r)
        lines += [f"- {summary_line(r)}",
                  f"  - 判定：{'✅ 可采纳' if j['accept'] else '❌ 不采纳'}（{j['reason']}）；参数年度变更 {r['param_changes']} 次"]
    if conclusion:
        lines += [f"- 结论：{conclusion}"]
    with EXPERIMENTS.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
    return n
