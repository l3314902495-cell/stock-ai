"""Shared lifecycle for serialized research sessions; the remote tag is a mutex."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

BRANCH = "optimize/research"
LOCK_REF = "refs/tags/stockai-research-lock"
ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, input: str | None = None, check: bool = True) -> str:
    p = subprocess.run(args, cwd=ROOT, input=input.encode("utf-8") if input is not None else None,
                       capture_output=True, timeout=120)
    p.stdout = p.stdout.decode("utf-8", errors="replace")
    p.stderr = p.stderr.decode("utf-8", errors="replace")
    if check and p.returncode:
        raise RuntimeError(f"命令失败：{' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


def git(*args: str, **kwargs) -> str:
    return run("git", *args, **kwargs)


def state_path() -> Path:
    path = Path(git("rev-parse", "--git-path", "stockai-weekly-lock.json"))
    return path if path.is_absolute() else ROOT / path


def remote_lock() -> str:
    # Network/auth errors must never be treated as an absent lock.
    output = git("ls-remote", "--refs", "origin", LOCK_REF)
    return output.split()[0] if output else ""


def owned_lock() -> dict:
    path = state_path()
    if not path.exists():
        raise RuntimeError("没有本次会话锁；请先运行 bash scripts/start_weekly.sh")
    state = json.loads(path.read_text(encoding="utf-8"))
    if remote_lock() != state["sha"]:
        raise RuntimeError("远端锁与本地会话不一致，停止；不要强推或删除其他会话的锁")
    return state


def clean() -> None:
    if git("status", "--porcelain"):
        raise RuntimeError("有未提交或未跟踪文件；请先保存并提交，再开始会话")


def identity() -> None:
    if not git("config", "user.name", check=False):
        git("config", "user.name", "stock-ai")
    if not git("config", "user.email", check=False):
        git("config", "user.email", "stock-ai@users.noreply.github.com")


def acquire() -> None:
    path = state_path()
    if path.exists():
        raise RuntimeError("已有本地会话；恢复请用 start --resume，放弃请用 release（保留本地改动）")
    if remote_lock():
        raise RuntimeError("其他会话正在研究（远端锁存在），本次停止，不开展实验")
    token = str(uuid.uuid4())
    who = git("var", "GIT_COMMITTER_IDENT")
    tag = (f"object {git('rev-parse', 'HEAD')}\ntype commit\n"
           f"tag stockai-research-lock\ntagger {who}\n\nresearch session {token}\n")
    sha = git("mktag", input=tag)
    # Exclusive local creation prevents two starts in the same checkout.
    with path.open("x", encoding="utf-8") as f:
        json.dump({"sha": sha, "token": token, "created": time.time()}, f)
    try:
        # No force: remote tag creation is atomic, so only one worker wins.
        git("push", "origin", f"{sha}:{LOCK_REF}")
    except Exception:
        # Preserve local state if the push outcome is uncertain.
        if remote_lock() != sha:
            path.unlink()
        raise


def validate_records() -> int:
    text = (ROOT / "optimize/EXPERIMENTS.md").read_text(encoding="utf-8")
    ids = [int(x) for x in re.findall(r"^## #(\d+)\b", text, re.M)]
    board = json.loads((ROOT / "optimize/scoreboard.json").read_text(encoding="utf-8"))
    if ids != list(range(1, len(ids) + 1)) or board["experiments"] != len(ids):
        raise RuntimeError("实验编号重复/不连续或与记分牌不一致；先核对历史，不要自动覆盖")
    return len(ids)


def start(resume: bool = False) -> None:
    clean()
    identity()
    if resume:
        owned_lock()
    else:
        acquire()
    # Fetch after acquiring so accepted sessions see their predecessor's work.
    git("fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")
    refs = git("for-each-ref", "--format=%(refname)").splitlines()
    local, remote = f"refs/heads/{BRANCH}", f"refs/remotes/origin/{BRANCH}"
    if local in refs:
        git("checkout", BRANCH)
        if remote in refs:
            # Refuse divergence. Never reset or merge concurrent experiments.
            git("merge", "--ff-only", f"origin/{BRANCH}")
    elif remote in refs:
        git("checkout", "-b", BRANCH, "--track", f"origin/{BRANCH}")
    else:
        git("checkout", "-b", BRANCH, "origin/main")
    git("merge", "--no-edit", "origin/main")
    n = validate_records()
    print(f"当前分支：{BRANCH}；{git('log', '--oneline', '-1')}；下个实验编号：#{n + 1}")
    print("会话锁已持有。发布成功后自动释放；失败保留锁和本地提交，修复后重试。")


def release() -> None:
    state = owned_lock()
    git("push", f"--force-with-lease={LOCK_REF}:{state['sha']}", "origin", f":{LOCK_REF}")
    state_path().unlink()
    print("已释放本会话锁；本地文件和提交保留。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "release"])
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        start(args.resume) if args.action == "start" else release()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
