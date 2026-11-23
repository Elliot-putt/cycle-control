import subprocess
import time
from pathlib import Path

import pytest

from cyclecontrol.board import Board
from cyclecontrol.comments import CommentStore
from cyclecontrol.github import GithubError, PrState
from cyclecontrol.nuke import STEPS, Nuker, NukeRefused, verified_worktree
from cyclecontrol.sessions import SessionManager, read_registry
from tests.test_sessions import add_session, issue, settings_for


def linked_worktree(tmp_path: Path, name: str = "spp-1035") -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / ".git").write_text(f"gitdir: {tmp_path / 'repo-pp' / '.git' / 'worktrees' / name}\n")
    return path


class Runner:
    def __init__(self, status="", ahead="", fail=()):
        self.calls: list[tuple[list[str], str]] = []
        self.status = status
        self.ahead = ahead
        self.fail = set(fail)
        self.on_remove = None

    def __call__(self, argv, cwd="", **kwargs):
        self.calls.append((argv, cwd))
        key = " ".join(argv[:2]) if argv[0] == "just" else " ".join(argv[:3])
        out = ""
        if argv[:2] == ["git", "status"]:
            out = self.status
        elif argv[:2] == ["git", "log"]:
            out = self.ahead
        if argv[:2] == ["just", "worktree-remove"] and self.on_remove:
            self.on_remove()
        return subprocess.CompletedProcess(argv, 1 if key in self.fail else 0, out, "nope")


def make(tmp_path, monkeypatch, pr=None, **extra):
    settings = settings_for(tmp_path, linear_api_key="k", **extra)
    (tmp_path / "repo-pp").mkdir(exist_ok=True)
    manager = SessionManager(settings)
    board = Board(settings)
    store = CommentStore(tmp_path / "c.json")
    runner = Runner()
    closed, unassigned = [], []
    nuker = Nuker(
        settings,
        manager,
        board,
        store,
        run=runner,
        find_pr=lambda repo, branch: pr,
        close=lambda repo, n: closed.append((repo, n)),
        unassign=lambda i: unassigned.append(i),
    )
    return nuker, manager, board, store, runner, closed, unassigned


def test_the_worktree_guard_accepts_only_this_issues_linked_worktree(tmp_path):
    team = settings_for(tmp_path).team_for("PP")
    with pytest.raises(NukeRefused, match="not a linked"):
        (tmp_path / "spp-1035").mkdir()
        verified_worktree(team, issue())
    (tmp_path / "spp-1035" / ".git").mkdir()
    with pytest.raises(NukeRefused, match="not a linked"):
        verified_worktree(team, issue())
    (tmp_path / "spp-1035" / ".git").rmdir()
    (tmp_path / "spp-1035" / ".git").write_text("gitdir: /elsewhere\n")
    assert verified_worktree(team, issue()) == tmp_path / "spp-1035"


def test_the_worktree_guard_refuses_the_main_checkout_and_foreign_paths(tmp_path):
    team = settings_for(tmp_path).team_for("PP")
    repo = tmp_path / "repo-pp"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: x\n")
    (tmp_path / "spp-1035").symlink_to(repo)
    with pytest.raises(NukeRefused, match="main checkout"):
        verified_worktree(team, issue())
    (tmp_path / "spp-1035").unlink()
    outside = tmp_path / "elsewhere" / "spp-1035"
    outside.mkdir(parents=True)
    (outside / ".git").write_text("gitdir: x\n")
    (tmp_path / "spp-1035").symlink_to(outside)
    with pytest.raises(NukeRefused, match="not the spp-1035 worktree beside the repo"):
        verified_worktree(team, issue())
    (tmp_path / "spp-1035").unlink()
    (tmp_path / "spp-1035").symlink_to(repo / ".git")
    with pytest.raises(NukeRefused, match="main checkout"):
        verified_worktree(team, issue())


def test_nuke_runs_every_step_in_order_and_reports_each(tmp_path, monkeypatch):
    pr = PrState(7, "https://x/pull/7", "feature/pp-1035", "OPEN", "develop", "abc")
    nuker, manager, board, store, runner, closed, unassigned = make(tmp_path, monkeypatch, pr)
    worktree = linked_worktree(tmp_path)
    runner.on_remove = lambda: (worktree / ".git").unlink() or worktree.rmdir()
    session = add_session(manager, tmp_path, issue())
    store.numbers["PP-1035"] = 7
    store.pr("o/pp#7")
    board.issues = [issue()]
    board.mark_in_progress("PP-1035")
    results = nuker.nuke(issue())
    assert [(r.step, r.ok, r.skipped) for r in results] == [
        ("builder", True, False),
        ("pr", True, False),
        ("linear", True, False),
        ("worktree", True, False),
        ("board", True, False),
    ]
    assert session.stopped and manager.get("PP-1035") is None
    assert manager.request_start(issue(), "plan") == "nuked"
    assert read_registry(manager._settings.state_path)["sessions"] == []
    assert store.numbers == {} and store.peek("o/pp#7") is None
    assert closed == [("o/pp", 7)] and unassigned == ["PP-1035"]
    argv = [c[0][:2] for c in runner.calls]
    assert argv == [
        ["just", "worktree-down"],
        ["just", "worktree-remove"],
        ["git", "worktree"],
        ["git", "branch"],
    ]
    assert runner.calls[0][1] == str(worktree) and runner.calls[1][1] == str(tmp_path / "repo-pp")
    assert runner.calls[1][0] == ["just", "worktree-remove", "feature/pp-1035"]
    assert runner.calls[3][0] == ["git", "branch", "-d", "feature/pp-1035"]
    assert board.view() == []


def test_a_failed_step_does_not_stop_the_later_ones(tmp_path, monkeypatch):
    pr = PrState(7, "https://x/pull/7", "feature/pp-1035", "OPEN", "develop", "abc")
    nuker, _manager, _board, _store, runner, _closed, unassigned = make(tmp_path, monkeypatch, pr)
    linked_worktree(tmp_path)
    nuker._close = lambda repo, n: (_ for _ in ()).throw(GithubError("gh down"))
    runner.fail = {"just worktree-down", "git worktree remove"}
    results = {r.step: r for r in nuker.nuke(issue())}
    assert results["pr"].ok is False and "gh down" in results["pr"].detail
    assert unassigned == ["PP-1035"]
    assert (
        results["worktree"].ok is False
        and "git worktree remove failed" in results["worktree"].detail
    )
    assert results["board"].ok is True


def test_without_a_pr_or_worktree_the_steps_are_skipped_quietly(tmp_path, monkeypatch):
    nuker, _manager, _board, _store, runner, closed, _unassigned = make(tmp_path, monkeypatch, None)
    results = {r.step: r for r in nuker.nuke(issue())}
    assert results["pr"].skipped and results["pr"].detail == "no PR" and closed == []
    assert results["worktree"].skipped and results["worktree"].detail == "no worktree"
    assert runner.calls == [] and results["builder"].detail == "no builder to end"
    merged = PrState(7, "https://x/pull/7", "feature/pp-1035", "MERGED", "develop", "abc")
    nuker._find_pr = lambda repo, branch: merged
    assert nuker.nuke(issue())[1].detail == "PR #7 already merged" and closed == []


def test_the_preview_lists_the_pr_and_what_would_be_lost(tmp_path, monkeypatch):
    pr = PrState(7, "https://x/pull/7", "feature/pp-1035", "OPEN", "develop", "abc")
    nuker, manager, _board, _store, runner, _closed, _unassigned = make(tmp_path, monkeypatch, pr)
    runner.status = " M a.php\n?? b.php\n"
    runner.ahead = "abc one\ndef two\n123 three\n"
    linked_worktree(tmp_path)
    add_session(manager, tmp_path, issue(), phase="human")
    view = nuker.preview(issue())
    assert view["pr"] == {"number": 7, "url": "https://x/pull/7", "state": "OPEN"}
    assert (view["uncommitted"], view["unpushed"]) == (2, 3)
    assert view["worktree"] == str(tmp_path / "spp-1035") and view["builder"] == "waiting"
    (tmp_path / "spp-1035" / ".git").unlink()
    (tmp_path / "spp-1035" / ".git").mkdir()
    assert "not a linked" in nuker.preview(issue())["worktreeRefused"]


def test_a_refused_worktree_fails_that_step_only(tmp_path, monkeypatch):
    nuker, _manager, _board, _store, runner, _closed, unassigned = make(tmp_path, monkeypatch, None)
    (tmp_path / "spp-1035").mkdir()
    results = {r.step: r for r in nuker.nuke(issue())}
    assert results["worktree"].ok is False and "not a linked" in results["worktree"].detail
    assert runner.calls == [] and unassigned == ["PP-1035"] and results["board"].ok


def test_demo_mode_never_touches_github_linear_or_the_worktree(tmp_path, monkeypatch):
    pr = PrState(7, "https://x/pull/7", "feature/pp-1035", "OPEN", "develop", "abc")
    nuker, _m, _b, _s, runner, closed, unassigned = make(tmp_path, monkeypatch, pr, demo=True)
    linked_worktree(tmp_path)
    results = {r.step: r for r in nuker.nuke(issue())}
    assert all(results[k].skipped for k in ("pr", "linear", "worktree"))
    assert closed == [] and unassigned == [] and runner.calls == []
    assert nuker.preview(issue())["pr"] is None


def test_start_returns_before_the_slow_steps_and_refuses_a_second_nuke(tmp_path, monkeypatch):
    import threading

    pr = PrState(7, "https://x/pull/7", "feature/pp-1035", "OPEN", "develop", "abc")
    nuker, manager, _board, _store, runner, closed, _unassigned = make(tmp_path, monkeypatch, pr)
    linked_worktree(tmp_path)
    add_session(manager, tmp_path, issue())
    gate = threading.Event()
    inner = runner.__call__

    def slow(argv, cwd="", **kwargs):
        if argv[:2] == ["just", "worktree-down"]:
            gate.wait(5)
        return inner(argv, cwd=cwd, **kwargs)

    nuker._run = slow
    assert nuker.start(issue()) == "started"
    assert manager.get("PP-1035") is None and manager.request_start(issue(), "plan") == "nuked"
    assert nuker.start(issue()) == "running"
    view = nuker.view()["PP-1035"]
    assert view["done"] is False and view["steps"][0]["step"] == "builder"
    assert nuker.find_issue("PP-1035") == issue()
    gate.set()
    for _ in range(200):
        if nuker.job("PP-1035").done:
            break
        time.sleep(0.01)
    view = nuker.view()["PP-1035"]
    assert view["done"] and view["ok"] and [s["step"] for s in view["steps"]] == list(STEPS)
    assert closed == [("o/pp", 7)]
    nuker.dismiss("PP-1035")
    assert nuker.view() == {}
    assert nuker.start(issue()) == "started"


def test_a_failed_step_is_kept_in_the_view_until_dismissed(tmp_path, monkeypatch):
    nuker, _m, _b, _s, _runner, _closed, _unassigned = make(tmp_path, monkeypatch, None)
    nuker._unassign = lambda i: (_ for _ in ()).throw(RuntimeError("linear down"))
    nuker.start(issue())
    for _ in range(200):
        if nuker.job("PP-1035").done:
            break
        time.sleep(0.01)
    view = nuker.view()["PP-1035"]
    assert view["ok"] is False and view["failed"] == [{"step": "linear", "detail": "linear down"}]
    nuker.dismiss("PP-1035")
    assert "PP-1035" not in nuker.view()
