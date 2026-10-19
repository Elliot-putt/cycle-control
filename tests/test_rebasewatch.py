import pytest

from cyclecontrol import rebasewatch
from cyclecontrol.github import PrState
from cyclecontrol.rebase import RebaseCheck
from cyclecontrol.rebasewatch import RebaseWatcher
from cyclecontrol.sessions import SessionManager
from tests.test_sessions import add_session, issue, settings_for


class SendPty:
    def __init__(self):
        self.alive = True
        self.pid = 0
        self.last_output = 0.0
        self.sent: list[bytes] = []

    def is_alive(self):
        return self.alive

    def write(self, data):
        self.sent.append(data)


@pytest.fixture
def world(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, auto_rebase=True)
    manager = SessionManager(settings)
    child = issue("ABC-12", team="ABC")
    session = add_session(manager, tmp_path, child)
    session.pty = SendPty()
    session.base, session.base_sha = "feature/abc-11", "aaa"
    state = {
        "tips": {"feature/abc-11": "aaa", "main": "ddd"},
        "pr": None,
        "check": RebaseCheck(True),
        "notes": [],
    }
    monkeypatch.setattr(
        rebasewatch, "remote_sha", lambda repo, branch: state["tips"].get(branch, "")
    )
    monkeypatch.setattr(rebasewatch, "find_pr_any_state", lambda repo, branch: state["pr"])
    monkeypatch.setattr(rebasewatch, "_fetch", lambda *a: None)
    monkeypatch.setattr(rebasewatch, "check_rebase", lambda repo, ref, old, new: state["check"])
    monkeypatch.setattr(
        rebasewatch, "notify", lambda title, message: state["notes"].append((title, message))
    )
    monkeypatch.setattr(
        "cyclecontrol.sessions.threading.Timer",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    return RebaseWatcher(settings, manager), manager, session, state


def merged(branch="feature/abc-11"):
    return PrState(5, "https://x/pull/5", branch, "MERGED", "main", "zzz")


def test_nothing_happens_while_the_base_has_not_moved(world):
    watcher, _, session, _ = world
    assert watcher.poll() == []
    assert session.pty.sent == [] and session.attention == ""


def test_a_clean_base_move_instructs_the_builder_and_records_the_new_base(world):
    watcher, _, session, state = world
    state["tips"]["feature/abc-11"] = "bbb"
    ((issue_id, action),) = watcher.poll()
    assert (issue_id, action.kind, action.new_sha) == ("ABC-12", "rebase", "bbb")
    assert (session.base, session.base_sha) == ("feature/abc-11", "bbb")
    assert (session.git_dir / "spp-base").read_text() == "origin/feature/abc-11\n"
    assert "git rebase --onto origin/feature/abc-11 aaa" in action.text
    assert watcher.poll() == []


def test_a_conflict_marks_the_lane_needs_you_and_sends_the_agent_nothing(world):
    watcher, _, session, state = world
    state["tips"]["feature/abc-11"] = "bbb"
    state["check"] = RebaseCheck(False, ("b.txt", "c.txt"))
    ((_, action),) = watcher.poll()
    assert action.kind == "conflict"
    assert session.attention == "Rebase conflict onto feature/abc-11: b.txt, c.txt"
    assert session.outcome(20) == "waiting"
    assert session.pty.sent == []
    assert (session.base, session.base_sha) == ("feature/abc-11", "aaa")
    assert len(state["notes"]) == 1
    watcher.poll()
    assert len(state["notes"]) == 1


def test_marking_a_conflict_resolved_adopts_the_new_base(world):
    watcher, manager, session, state = world
    state["tips"]["feature/abc-11"] = "bbb"
    state["check"] = RebaseCheck(False, ("b.txt",))
    watcher.poll()
    manager.clear_attention("ABC-12")
    assert (session.attention, session.base, session.base_sha) == ("", "feature/abc-11", "bbb")
    assert watcher.poll() == []


def test_a_merged_base_pr_retargets_to_the_default_branch_when_clean(world):
    watcher, _, session, state = world
    state["pr"] = merged()
    state["tips"].pop("feature/abc-11")
    ((_, action),) = watcher.poll()
    assert (action.kind, action.new_base, action.new_sha) == ("rebase", "main", "ddd")
    assert (session.base, session.base_sha) == ("main", "ddd")
    assert "gh pr edit --base main" in action.text
    assert watcher.poll() == []


def test_a_merged_base_pr_with_a_conflict_needs_you(world):
    watcher, _, session, state = world
    state["pr"] = merged()
    state["tips"].pop("feature/abc-11")
    state["check"] = RebaseCheck(False, ("x.txt",))
    watcher.poll()
    assert session.attention == "Rebase conflict onto main: x.txt" and session.pty.sent == []


def test_builders_based_on_the_default_branch_and_stopped_ones_are_left_alone(world):
    watcher, _, session, state = world
    state["tips"]["main"] = "eee"
    session.base, session.base_sha = "main", "ddd"
    assert watcher.poll() == []
    session.base, session.base_sha = "feature/abc-11", "aaa"
    state["tips"]["feature/abc-11"] = "bbb"
    session.pty.alive = False
    assert watcher.poll() == []
