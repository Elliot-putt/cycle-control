import os
import stat
import subprocess
import time

import pytest

from cyclecontrol import locks
from cyclecontrol import sessions as sessions_module
from cyclecontrol.locks import LOCK_NAME, BuilderLockedError, Procs
from cyclecontrol.sessions import SessionManager
from tests.test_comments import c, pr, thread
from tests.test_sessions import add_session, issue, settings_for


class FakeProcs(Procs):
    def __init__(self, running=(), strays=()):
        self.running = set(running)
        self.strays = list(strays)

    def claude_running(self, pid):
        return pid in self.running

    def claude_pids_in(self, worktree):
        return list(self.strays)


@pytest.fixture
def dirs(tmp_path):
    git_dir = tmp_path / "git"
    worktree = tmp_path / "wt"
    git_dir.mkdir()
    worktree.mkdir()
    return git_dir, worktree


def test_acquire_writes_the_lock_and_the_pid_is_filled_in_after_the_spawn(dirs):
    git_dir, worktree = dirs
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    assert (git_dir / LOCK_NAME).exists()
    locks.set_pid(git_dir, 4242)
    assert locks._read(git_dir)["pid"] == 4242 and locks._read(git_dir)["issue"] == "PP-1"


def test_a_second_builder_is_refused_while_the_first_is_alive(dirs):
    git_dir, worktree = dirs
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    locks.set_pid(git_dir, 4242)
    with pytest.raises(BuilderLockedError, match="pid 4242"):
        locks.acquire(git_dir, worktree, "PP-1", FakeProcs(running={4242}))


def test_a_lock_whose_process_is_gone_is_stale_and_replaced(dirs):
    git_dir, worktree = dirs
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    locks.set_pid(git_dir, 4242)
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs(running=set()))
    assert locks._read(git_dir)["pid"] == 0


def test_a_reused_pid_that_is_not_claude_does_not_hold_the_lock(dirs):
    git_dir, worktree = dirs
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    locks.set_pid(git_dir, 99)
    assert locks.holder(git_dir, FakeProcs(running=set())) is None


def test_a_claude_started_outside_the_app_in_the_worktree_blocks_a_new_builder(dirs):
    git_dir, worktree = dirs
    with pytest.raises(BuilderLockedError, match="pid 555"):
        locks.acquire(git_dir, worktree, "PP-1", FakeProcs(strays=[555]))
    assert not (git_dir / LOCK_NAME).exists()


def test_a_builder_that_is_still_starting_holds_the_lock_only_while_its_app_lives(dirs):
    git_dir, worktree = dirs
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    assert locks.holder(git_dir, FakeProcs()) is not None
    lock = locks._read(git_dir)
    (git_dir / LOCK_NAME).write_text(__import__("json").dumps({**lock, "at": time.time() - 1000}))
    assert locks.holder(git_dir, FakeProcs()) is None
    (git_dir / LOCK_NAME).write_text(__import__("json").dumps({**lock, "app": 2**22 + 12345}))
    assert locks.holder(git_dir, FakeProcs()) is None


def test_release_drops_only_your_own_lock(dirs):
    git_dir, worktree = dirs
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    locks.set_pid(git_dir, 4242)
    locks.release(git_dir, 9999)
    assert (git_dir / LOCK_NAME).exists()
    locks.release(git_dir, 4242)
    assert not (git_dir / LOCK_NAME).exists()
    locks.release(git_dir)


def test_confirm_gone_checks_the_lock_and_the_process_table(dirs):
    git_dir, worktree = dirs
    assert locks.confirm_gone(git_dir, worktree, FakeProcs()) == (True, "")
    locks.acquire(git_dir, worktree, "PP-1", FakeProcs())
    locks.set_pid(git_dir, 4242)
    free, why = locks.confirm_gone(git_dir, worktree, FakeProcs(running={4242}))
    assert not free and "4242" in why
    free, why = locks.confirm_gone(git_dir, worktree, FakeProcs(strays=[777]))
    assert not free and "777" in why
    assert locks.confirm_gone(git_dir, worktree, FakeProcs()) == (True, "")


def test_the_real_process_check_sees_a_live_claude_and_not_a_dead_or_other_one(tmp_path):
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    proc = subprocess.Popen([str(script)], cwd=tmp_path)
    try:
        real = Procs()
        assert real.claude_running(proc.pid) is True
        assert real.claude_running(os.getpid()) is False
        assert real.claude_running(0) is False
    finally:
        proc.kill()
        proc.wait()
    assert Procs().claude_running(proc.pid) is False


class StubPty:
    def __init__(self, argv, cwd, env=None):
        self.pid = 31337
        self.last_output = 0.0
        self.alive = True

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def write(self, data):
        pass


@pytest.fixture
def start_rig(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    manager = SessionManager(settings)
    git_dir = tmp_path / "git-abc"
    worktree = tmp_path / "abc-12"
    git_dir.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(sessions_module, "ensure_worktree", lambda team, i, base: worktree)
    monkeypatch.setattr(sessions_module, "git_dir", lambda path: git_dir)
    monkeypatch.setattr(sessions_module, "remote_sha", lambda repo, branch: "")
    monkeypatch.setattr(sessions_module, "PtySession", StubPty)
    monkeypatch.setattr(locks.PROCS, "claude_running", lambda pid: False)
    monkeypatch.setattr(locks.PROCS, "claude_pids_in", lambda path: [])
    return manager, git_dir, worktree


def test_starting_a_builder_takes_the_lock_with_its_pid_and_stopping_releases_it(start_rig):
    manager, git_dir, _ = start_rig
    manager.start(issue("ABC-12", team="ABC"), "plan")
    assert locks._read(git_dir)["pid"] == 31337
    manager.stop("ABC-12")
    assert not (git_dir / LOCK_NAME).exists()


def test_a_second_start_in_a_worktree_with_a_live_builder_is_refused_without_spawning(
    start_rig, monkeypatch
):
    manager, _git_dir, _ = start_rig
    manager.start(issue("ABC-12", team="ABC"), "plan")
    spawned = []
    monkeypatch.setattr(
        sessions_module, "PtySession", lambda *a, **k: spawned.append(1) or StubPty(*a)
    )
    monkeypatch.setattr(locks.PROCS, "claude_running", lambda pid: pid == 31337)
    manager._sessions.clear()
    with pytest.raises(BuilderLockedError):
        manager.start(issue("ABC-12", team="ABC"), "plan")
    assert spawned == []


def test_a_failed_spawn_does_not_leave_a_lock_behind(start_rig, monkeypatch):
    manager, git_dir, _ = start_rig

    class Boom(StubPty):
        def start(self):
            raise OSError("no claude")

    monkeypatch.setattr(sessions_module, "PtySession", Boom)
    with pytest.raises(OSError, match="no claude"):
        manager.start(issue("ABC-12", team="ABC"), "plan")
    assert not (git_dir / LOCK_NAME).exists()


def test_release_dead_drops_the_lock_of_an_ended_builder(start_rig):
    manager, git_dir, _ = start_rig
    session = manager.start(issue("ABC-12", team="ABC"), "plan")
    manager.release_dead()
    assert (git_dir / LOCK_NAME).exists()
    session.pty.alive = False
    manager.release_dead()
    assert not (git_dir / LOCK_NAME).exists()


# ---- the resume fallback
def comment_rig(tmp_path, monkeypatch):
    from cyclecontrol.comments import CommentStore
    from cyclecontrol.commentwatch import CommentWatcher

    settings = settings_for(tmp_path)
    manager = SessionManager(settings)
    session = add_session(manager, tmp_path, issue("ABC-12", team="ABC"))
    session.pty.pid = 800
    monkeypatch.setattr(
        "cyclecontrol.sessions.threading.Timer",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    raw = {"value": pr()}
    watcher = CommentWatcher(
        settings,
        manager,
        CommentStore(tmp_path / "c.json"),
        lambda repo, n: raw["value"],
        lambda repo, branch: type("P", (), {"number": 7})(),
        lambda: clock["now"],
    )
    clock = {"now": 1000.0}
    watcher.poll()
    raw["value"] = pr(threads=[thread("T1", [c(1, "sam", "Rename")])])
    watcher.poll()
    session.pty.alive = False
    return watcher, manager, session, clock


def test_an_ended_builder_is_not_resumed_while_its_old_process_is_still_there(
    tmp_path, monkeypatch
):
    watcher, _manager, session, _clock = comment_rig(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        SessionManager, "revive_for_comments", lambda self, *a: calls.append("resume") or "started"
    )
    monkeypatch.setattr(locks.PROCS, "claude_running", lambda pid: pid == 800)
    (session.git_dir / LOCK_NAME).write_text('{"pid": 800, "issue": "ABC-12"}')
    watcher.tick()
    watcher.tick()
    assert calls == []
    monkeypatch.setattr(locks.PROCS, "claude_running", lambda pid: False)
    monkeypatch.setattr(locks.PROCS, "claude_pids_in", lambda path: [])
    watcher.tick()
    assert calls == ["resume"]


def test_the_new_builder_waits_for_the_old_process_to_be_really_gone(tmp_path, monkeypatch):
    watcher, _manager, session, clock = comment_rig(tmp_path, monkeypatch)
    started = []

    def revive(self, issue_, mode, base, branch):
        newborn = add_session(self, tmp_path, issue_)
        newborn.pty.alive = False
        newborn.worktree = session.worktree
        newborn.git_dir = session.git_dir
        return "started"

    monkeypatch.setattr(SessionManager, "revive_for_comments", revive)
    monkeypatch.setattr(
        SessionManager,
        "start_fresh_for_pr",
        lambda self, i, mode, base, prompt: started.append(prompt) or "started",
    )
    monkeypatch.setattr(locks.PROCS, "claude_running", lambda pid: False)
    monkeypatch.setattr(locks.PROCS, "claude_pids_in", lambda path: [])
    watcher.tick()
    stray = {"pids": [901]}
    monkeypatch.setattr(locks.PROCS, "claude_pids_in", lambda path: stray["pids"])
    clock["now"] += 5
    watcher.tick()
    watcher.tick()
    assert started == []
    stray["pids"] = []
    watcher.tick()
    assert len(started) == 1 and "PR #7" in started[0]


def test_a_locked_worktree_during_resume_is_retried_not_replaced(tmp_path, monkeypatch):
    watcher, _manager, _session, _clock = comment_rig(tmp_path, monkeypatch)
    attempts = []

    def revive(self, *a):
        attempts.append(1)
        raise BuilderLockedError("held")

    monkeypatch.setattr(SessionManager, "revive_for_comments", revive)
    started = []
    monkeypatch.setattr(SessionManager, "start_fresh_for_pr", lambda self, *a: started.append(1))
    monkeypatch.setattr(locks.PROCS, "claude_running", lambda pid: False)
    monkeypatch.setattr(locks.PROCS, "claude_pids_in", lambda path: [])
    watcher.tick()
    watcher.tick()
    assert len(attempts) == 2 and started == []
