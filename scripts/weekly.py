"""Shared lifecycle for serialized research sessions; a remote branch is the mutex.

锁是远端分支 stockai-research-lock，它只增不删：最新提交说明以 LOCKED 开头表示被占用，
RELEASED 表示空闲。取锁 = 在观察到的空闲提交上追加 LOCKED 提交并普通推送；如果期间别人先取了锁，
我们的推送不是快进，会被远端拒绝，因此仍是原子的互斥。释放 = 追加 RELEASED 提交。
整个过程只用普通推送（不删除、不强推），因为云端会话的 git 代理禁止推送 tag、删除和强推远端引用。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

BRANCH = "optimize/research"
LOCK_REF = "refs/heads/stockai-research-lock"
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


def lock_tip() -> str:
    """锁分支当前的提交（不存在时为空）。网络/认证错误会抛出，绝不当作"没有锁"。"""
    output = git("ls-remote", "--refs", "origin", LOCK_REF)
    return output.split()[0] if output else ""


def _held(sha: str) -> bool:
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT, capture_output=True).returncode:
        git("fetch", "--no-tags", "-q", "origin", LOCK_REF)  # 本地还没有这个提交对象
    return git("log", "-1", "--format=%s", sha).startswith("LOCKED")


def remote_lock() -> str:
    """被占用时返回锁提交，空闲时返回空字符串。"""
    tip = lock_tip()
    return tip if tip and _held(tip) else ""


def _lock_commit(message: str, parent: str) -> str:
    tree = git("mktree", input="")
    return git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message)


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
    tip = lock_tip()  # 同一次观察既用来判断是否空闲，也作为新锁提交的父提交（CAS 的关键）
    if tip and _held(tip):
        raise RuntimeError("其他会话正在研究（远端锁被占用），本次停止，不开展实验")
    token = str(uuid.uuid4())
    who = git("var", "GIT_COMMITTER_IDENT")
    sha = _lock_commit(f"LOCKED research session {token}\n\n{who}", tip)
    # Exclusive local creation prevents two starts in the same checkout.
    with path.open("x", encoding="utf-8") as f:
        json.dump({"sha": sha, "token": token, "created": time.time()}, f)
    try:
        # No force: a fast-forward from the observed tip (or creating the branch) is atomic, so only one worker wins.
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
    sha = _lock_commit(f"RELEASED research session {state.get('token', '')}", state["sha"])
    git("push", "origin", f"{sha}:{LOCK_REF}")  # 快进；若锁被别人改动过会被拒绝
    state_path().unlink()
    print("已释放本会话锁；本地文件和提交保留。")


def force_release() -> None:
    """仅供所有者在确认原会话已彻底停止后使用：在当前锁提交上追加 RELEASED（不删除、不强推）。"""
    tip = lock_tip()
    if not tip or not _held(tip):
        print("锁本来就是空闲的。")
        return
    sha = _lock_commit(f"RELEASED by owner (forced), previous {tip}", tip)
    git("push", "origin", f"{sha}:{LOCK_REF}")
    if state_path().exists():
        state_path().unlink()
    print(f"已强制释放锁（原锁提交 {tip[:10]} 已存档在锁分支历史中）。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "release", "force-release"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--yes", action="store_true", help="force-release 需要确认")
    args = parser.parse_args()
    try:
        if args.action == "start":
            start(args.resume)
        elif args.action == "release":
            release()
        elif not args.yes:
            parser.exit(1, "force-release 会释放别的会话持有的锁，请先确认原会话已停止，再加 --yes\n")
        else:
            force_release()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
