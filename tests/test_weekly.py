"""Real local Git remotes exercise lifecycle and concurrency without GitHub."""
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
weekly = importlib.import_module("weekly")
publisher = importlib.import_module("publish_weekly")


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    remote, a, b = [tmp_path / n for n in ("remote.git", "a", "b")]
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(a))
    git(a, "config", "user.name", "Research test")
    git(a, "config", "user.email", "research@example.test")
    (a / "optimize").mkdir()
    (a / "optimize/EXPERIMENTS.md").write_text("## #1 example\n", encoding="utf-8")
    (a / "optimize/scoreboard.json").write_text('{"groups":{},"experiments":1}', encoding="utf-8")
    git(a, "add", ".")
    git(a, "commit", "-m", "seed")
    git(a, "remote", "add", "origin", str(remote))
    git(a, "push", "origin", "main")
    git(tmp_path, "clone", "-b", "main", str(remote), str(b))
    monkeypatch.setattr(weekly, "ROOT", a)
    monkeypatch.setattr(publisher, "ROOT", a)
    return remote, a, b


def test_serial_sessions_resume_from_published_research(repos, monkeypatch):
    _, a, b = repos
    weekly.start()
    first_lock = weekly.remote_lock()
    with pytest.raises(RuntimeError, match="已有本地会话"):
        weekly.start()
    monkeypatch.setattr(weekly, "ROOT", b)
    with pytest.raises(RuntimeError, match="其他会话"):
        weekly.start()
    assert git(b, "branch", "--show-current") == "main"
    monkeypatch.setattr(weekly, "ROOT", a)
    (a / "result.txt").write_text("retained experiment", encoding="utf-8")
    git(a, "add", ".")
    git(a, "commit", "-m", "experiment")
    git(a, "push", "origin", weekly.BRANCH)
    weekly.release()
    monkeypatch.setattr(weekly, "ROOT", b)
    weekly.start()
    assert (b / "result.txt").read_text() == "retained experiment"
    assert weekly.remote_lock() != first_lock
    weekly.release()


def test_start_keeps_unpushed_commit(repos):
    _, a, _ = repos
    weekly.start()
    git(a, "push", "origin", weekly.BRANCH)
    (a / "local.txt").write_text("unpublished", encoding="utf-8")
    git(a, "add", ".")
    git(a, "commit", "-m", "unpublished")
    head = git(a, "rev-parse", "HEAD")
    weekly.start(resume=True)
    assert git(a, "rev-parse", "HEAD") == head
    weekly.release()


def test_dirty_start_cannot_acquire_lock(repos):
    _, a, _ = repos
    (a / "unsaved.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(RuntimeError, match="未提交"):
        weekly.start()
    assert not weekly.remote_lock()
    assert (a / "unsaved.txt").read_text() == "keep me"


def test_divergence_stops_without_losing_local_commit(repos):
    remote, a, b = repos
    weekly.start()
    git(a, "push", "origin", weekly.BRANCH)
    git(b, "fetch", "origin")
    git(b, "checkout", "-b", weekly.BRANCH, f"origin/{weekly.BRANCH}")
    for repo, name in ((a, "local"), (b, "legacy")):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.test")
        (repo / f"{name}.txt").write_text(name)
        git(repo, "add", ".")
        git(repo, "commit", "-m", name)
    git(b, "push", "origin", weekly.BRANCH)
    head = git(a, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError):
        weekly.start(resume=True)
    assert git(a, "rev-parse", "HEAD") == head
    assert weekly.remote_lock()
    weekly.release()


def test_release_refuses_someone_elses_lock(repos):
    _, a, _ = repos
    weekly.start()
    state = weekly.state_path()
    own = state.read_text()
    state.write_text(json.dumps({"sha": "0" * 40}))
    with pytest.raises(RuntimeError, match="不一致"):
        weekly.release()
    assert weekly.remote_lock()
    state.write_text(own)
    weekly.release()


@pytest.mark.parametrize("ids,count", [("## #1 a\n## #1 b", 2), ("## #2 a", 1), ("## #1 a", 2)])
def test_invalid_experiment_numbers_stop(repos, ids, count):
    _, a, _ = repos
    (a / "optimize/EXPERIMENTS.md").write_text(ids)
    (a / "optimize/scoreboard.json").write_text(json.dumps({"experiments": count}))
    with pytest.raises(RuntimeError, match="编号"):
        weekly.validate_records()


def test_publish_retry_does_not_duplicate_comment(repos, tmp_path, monkeypatch):
    _, a, _ = repos
    weekly.start()
    report = tmp_path / "report.md"
    report.write_text("本周记录", encoding="utf-8")
    comments = []
    creates = []
    pr = {"number": 1, "html_url": "https://example.test/pr/1"}

    def api(endpoint, payload=None, paginate=False):
        if "/pulls?" in endpoint:
            return [] if not creates else [pr]
        if endpoint.endswith("/pulls"):
            creates.append(pr)
            return pr
        if payload is not None:
            comments.append(payload)
            raise RuntimeError("response lost after GitHub accepted comment")
        return comments

    monkeypatch.setattr(publisher, "api", api)
    with pytest.raises(RuntimeError, match="response lost"):
        publisher.publish(report)
    assert weekly.remote_lock()
    assert publisher.publish(report) == pr["html_url"]
    assert len(comments) == len(creates) == 1
    assert not weekly.remote_lock()


def test_failed_push_does_not_create_pr_or_comment(repos, tmp_path, monkeypatch):
    _, a, b = repos
    weekly.start()
    git(a, "push", "origin", weekly.BRANCH)
    git(b, "fetch", "origin")
    git(b, "push", "origin", f"main:refs/heads/{weekly.BRANCH}")
    report = tmp_path / "report.md"
    report.write_text("report")
    original = publisher.git

    def reject_push(*args, **kwargs):
        if args[0] == "push":
            raise RuntimeError("non-fast-forward")
        return original(*args, **kwargs)

    monkeypatch.setattr(publisher, "git", reject_push)
    monkeypatch.setattr(publisher, "api", lambda *a, **k: pytest.fail("API called after failed push"))
    with pytest.raises(RuntimeError, match="non-fast-forward"):
        publisher.publish(report)
    assert weekly.remote_lock()
    weekly.release()


def test_atomic_lock_push_rejects_worker_with_stale_observation(repos, monkeypatch):
    _, a, b = repos
    weekly.start()
    owner_sha = weekly.remote_lock()
    monkeypatch.setattr(weekly, "ROOT", b)
    weekly.identity()
    # Both workers could have observed no tag before either push arrived.
    real_remote_lock = weekly.remote_lock
    monkeypatch.setattr(weekly, "remote_lock", lambda: "")
    with pytest.raises(RuntimeError):
        weekly.acquire()
    monkeypatch.setattr(weekly, "remote_lock", real_remote_lock)
    assert weekly.remote_lock() == owner_sha
    assert not weekly.state_path().exists()
    monkeypatch.setattr(weekly, "ROOT", a)
    weekly.release()


def test_publish_rejects_untracked_strategy(repos, tmp_path):
    _, a, _ = repos
    weekly.start()
    (a / "forgotten_strategy.py").write_text("# not committed")
    report = tmp_path / "report.md"
    report.write_text("report")
    with pytest.raises(RuntimeError, match="未跟踪"):
        publisher.publish(report)
    assert not git(a, "ls-remote", "--heads", "origin", weekly.BRANCH)
    weekly.release()


def test_lock_branch_is_append_only(repos):
    """云端 git 代理禁止删除/强推远端引用，锁只能靠追加提交来释放。"""
    remote, a, _ = repos
    weekly.start()
    held = weekly.lock_tip()
    weekly.release()
    tip = weekly.lock_tip()
    assert tip and tip != held and not weekly.remote_lock()
    assert git(a, "log", "-1", "--format=%s", tip).startswith("RELEASED")
    assert git(a, "merge-base", "--is-ancestor", held, tip) == ""  # 释放是快进，没有改写历史


def test_force_release_by_owner(repos, monkeypatch):
    _, a, b = repos
    weekly.start()
    monkeypatch.setattr(weekly, "ROOT", b)
    weekly.identity()
    with pytest.raises(RuntimeError, match="其他会话"):
        weekly.acquire()
    weekly.force_release()
    assert not weekly.remote_lock()
    weekly.start()
    assert weekly.remote_lock()
    weekly.release()
