"""每周优化会话结束时运行：推送 optimize/research，确保长期 PR 存在，并把周报作为评论发到 PR 上。

用法：python scripts/publish_weekly.py report.md
（云端会话不能用 GitHub GraphQL，所以这里只用 REST：gh api。report.md 不要提交。）
"""
from __future__ import annotations

import json
import subprocess
import sys

REPO = "l3314902495-cell/stock-ai"
BRANCH = "optimize/research"
PR_TITLE = "持续优化：规则策略研究（长期 PR）"
PR_BODY = """这是持续优化的长期 PR。每周的定时会话都在 `optimize/research` 分支上接着做一个实验，并在这里以评论的形式发周报。

- 目标与规则：GOALS.md
- 全部实验记录：optimize/EXPERIMENTS.md
- 待验证假设：optimize/BACKLOG.md
- 当前冠军：optimize/scoreboard.json（或运行 `stockai bt board`）

可以随时合并；合并后下一次会话会从新的 main 继续在同一分支上工作。"""


def run(*args: str, check: bool = True) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"命令失败：{' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("用法：python scripts/publish_weekly.py report.md")
    report = open(sys.argv[1], encoding="utf-8").read()
    if run("git", "rev-parse", "--abbrev-ref", "HEAD") != BRANCH:
        sys.exit(f"请在 {BRANCH} 分支上运行（先执行 bash scripts/start_weekly.sh）")
    if run("git", "status", "--porcelain", "--untracked-files=no"):
        sys.exit("还有未提交的改动，请先提交")
    for attempt in range(4):
        r = subprocess.run(["git", "push", "-u", "origin", BRANCH], capture_output=True, text=True)
        if r.returncode == 0:
            break
        if attempt == 3:
            sys.exit(f"推送失败：{r.stderr}")
        subprocess.run(["sleep", str(2 ** (attempt + 1))])
    owner = REPO.split("/")[0]
    prs = json.loads(run("gh", "api", f"repos/{REPO}/pulls?state=open&head={owner}:{BRANCH}"))
    if prs:
        num, url = prs[0]["number"], prs[0]["html_url"]
    else:
        pr = json.loads(run("gh", "api", f"repos/{REPO}/pulls", "-f", f"title={PR_TITLE}", "-f", f"head={BRANCH}",
                            "-f", "base=main", "-f", f"body={PR_BODY}"))
        num, url = pr["number"], pr["html_url"]
    run("gh", "api", f"repos/{REPO}/issues/{num}/comments", "-f", f"body={report}")
    print(f"已推送并发布周报：{url}")


if __name__ == "__main__":
    main()
