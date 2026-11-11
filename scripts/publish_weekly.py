"""Push research and publish an idempotent PR report using REST only.

Usage: python scripts/publish_weekly.py report.md
Failures retain the session lock; fix the error and retry the same report.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from weekly import BRANCH, ROOT, git, owned_lock, release, run, validate_records

REPO = "l3314902495-cell/stock-ai"
PR_TITLE = "持续优化：规则策略研究（长期 PR）"
PR_BODY = """每周继续 `optimize/research` 的研究，周报以评论追加在本 PR。

- 目标：GOALS.md
- 实验：optimize/EXPERIMENTS.md
- 假设：optimize/BACKLOG.md
- 暂定冠军：optimize/scoreboard.json（待所有者审核）
- 操作及恢复：scripts/RESEARCH.md

由所有者决定何时合并。合并后保留研究分支；PR 若已合并，下一次有新提交时自动新建承接 PR。"""


def api(endpoint: str, payload: dict | None = None, paginate: bool = False):
    args = ["gh", "api", endpoint]
    if paginate:
        args += ["--paginate", "--slurp"]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    result = json.loads(run(*args, input=json.dumps(payload, ensure_ascii=False) if payload is not None else None))
    return [item for page in result for item in page] if paginate else result


def research_pr() -> dict:
    owner = REPO.split("/")[0]
    endpoint = f"repos/{REPO}/pulls?state=open&base=main&head={owner}:{BRANCH}"
    prs = api(endpoint)
    if len(prs) > 1:
        raise RuntimeError("发现多个研究 PR，请核对后再发布")
    if prs:
        return prs[0]
    try:
        return api(f"repos/{REPO}/pulls", {"title": PR_TITLE, "head": BRANCH, "base": "main", "body": PR_BODY})
    except RuntimeError:
        # A retry or another publisher may already have created it.
        prs = api(endpoint)
        if len(prs) == 1:
            return prs[0]
        raise


def publish(report_path: Path) -> str:
    report = report_path.read_text(encoding="utf-8").strip()
    if not report:
        raise RuntimeError("周报不能为空")
    if git("branch", "--show-current") != BRANCH:
        raise RuntimeError(f"请先切到 {BRANCH} 分支")
    owned_lock()
    if git("status", "--porcelain"):
        raise RuntimeError("还有未提交或未跟踪成果，请先提交；周报放在忽略的 report.md 或仓库外")
    if (report_path.resolve().is_relative_to(ROOT.resolve())
            and git("ls-files", "--", str(report_path.resolve().relative_to(ROOT.resolve())).replace("\\", "/"))):
        raise RuntimeError("周报应为临时文件，不应提交到仓库")
    validate_records()
    # Competing updates require reconciliation, never force push.
    git("push", "-u", "origin", BRANCH)
    pr = research_pr()
    marker = f"<!-- stockai-weekly:{git('rev-parse', 'HEAD')}:{hashlib.sha256(report.encode()).hexdigest()} -->"
    endpoint = f"repos/{REPO}/issues/{pr['number']}/comments"
    comments = api(endpoint + "?per_page=100", paginate=True)
    if not any(marker in c.get("body", "") for c in comments):
        api(endpoint, {"body": report + "\n\n" + marker})
    release()
    return pr["html_url"]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("用法：python scripts/publish_weekly.py report.md")
    try:
        print(f"已推送并发布周报：{publish(Path(sys.argv[1]))}")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"{exc}\n发布未完成；保留会话锁和本地提交，请修复后重试。")


if __name__ == "__main__":
    main()
