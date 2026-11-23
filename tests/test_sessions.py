import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from cyclecontrol import worktrees
from cyclecontrol.config import Settings, load_teams
from cyclecontrol.models import Blocker, Issue
from cyclecontrol.sessions import (
    NotYoursError,
    Session,
    SessionManager,
    UnmappedTeamError,
    builder_argv,
    builder_prompt,
    flags_of,
    read_registry,
    write_registry,
)


def issue(issue_id="PP-1035", team="PP", **extra) -> Issue:
    return Issue(
        issue_id,
        "Dates",
        f"https://x/{issue_id}",
        f"feature/{issue_id.lower()}",
        "Todo",
        "unstarted",
        "E",
        False,
        True,
        team_key=team,
        **extra,
    )


def make_teams(tmp_path: Path) -> Path:
    path = tmp_path / "teams.json"
    path.write_text(
        json.dumps(
            {
                "PP": {
                    "repo": str(tmp_path / "repo-pp"),
                    "github": "o/pp",
                    "agent": "spp-builder",
                    "baseBranch": "develop",
                    "worktreePrefix": "spp",
                },
                "ABC": {
                    "repo": str(tmp_path / "repo-abc"),
                    "github": "o/abc",
                    "agent": "abc-builder",
                    "baseBranch": "main",
                    "worktreePrefix": "abc",
                },
            }
        )
    )
    return path


def settings_for(tmp_path: Path, **extra) -> Settings:
    return Settings(
        state_file=str(tmp_path / "s.json"), teams_file=str(make_teams(tmp_path)), **extra
    )


class Pty:
    def __init__(self, alive=True):
        self.alive = alive
        self.pid = 0
        self.last_output = 0.0
        self.terminated = False

    def is_alive(self):
        return self.alive

    def tail(self):
        return b""

    def terminate(self):
        self.terminated = True
        self.alive = False


def add_session(manager, tmp_path, item, phase="plan", alive=True):
    gitdir = tmp_path / f"git-{item.id}"
    gitdir.mkdir(exist_ok=True)
    (gitdir / "spp-phase.json").write_text(json.dumps({"phase": phase}))
    session = Session(item, "plan", tmp_path, gitdir, Pty(alive))
    manager._sessions[item.id] = session
    return session


def test_team_mapping_gives_repo_agent_base_and_worktree_per_team(tmp_path):
    teams = load_teams(make_teams(tmp_path))
    assert teams["PP"].agent == "spp-builder" and teams["ABC"].agent == "abc-builder"
    assert teams["ABC"].base_branch == "main"
    assert teams["ABC"].worktree_path("ABC-12") == tmp_path / "abc-12"
    assert load_teams(tmp_path / "missing.json") == {}


def test_a_team_without_a_mapping_cannot_start(tmp_path):
    manager = SessionManager(settings_for(tmp_path))
    with pytest.raises(UnmappedTeamError, match="No repo mapped for team SPWEB"):
        manager.request_start(issue("SPWEB-1", team="SPWEB"), "plan")


def test_issue_team_falls_back_to_the_identifier_prefix():
    assert issue("QQ-9", team="").team == "QQ"


def test_builder_prompt_and_argv_carry_the_base_and_the_teams_agent(tmp_path):
    settings = settings_for(tmp_path, claude_model="sonnet")
    team = settings.team_for("ABC")
    prompt = builder_prompt(issue("ABC-5", team="ABC"), Path("/w"), "plan", "feature/abc-4")
    assert (
        "Base branch: feature/abc-4" in prompt
        and "--base feature/abc-4" in prompt
        and "Plan-first" in prompt
    )
    fresh = builder_argv(settings, team, prompt, resume=False)
    assert fresh[:3] == ["claude", "--agent", "abc-builder"] and fresh[-1] == prompt
    resumed = builder_argv(settings, team, prompt, resume=True)
    assert resumed[-1] == "--continue" and prompt not in resumed
    nudged = builder_argv(settings, team, prompt, resume=True, resume_note="carry on")
    assert nudged[-2:] == ["--continue", "carry on"] and prompt not in nudged


def test_ensure_worktree_branches_from_the_given_base_and_reuses_an_existing_one(
    tmp_path, monkeypatch
):
    team = settings_for(tmp_path).team_for("ABC")
    calls: list[tuple[str, ...]] = []

    def fake_git(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(worktrees, "_git", fake_git)
    path = worktrees.ensure_worktree(team, issue("ABC-12", team="ABC"), "feature/abc-11")
    assert path == tmp_path / "abc-12"
    assert calls[0][-3:] == ("fetch", "origin", "feature/abc-11")
    assert calls[1][2:] == (
        "worktree",
        "add",
        str(path),
        "-b",
        "feature/abc-12",
        "origin/feature/abc-11",
    )
    path.mkdir()
    calls.clear()
    assert worktrees.ensure_worktree(team, issue("ABC-12", team="ABC"), "main") == path
    assert calls == []


def test_registry_round_trip_keeps_the_queue_and_reads_the_old_list_format(tmp_path):
    path = tmp_path / "state" / "s.json"
    assert read_registry(path) == {"sessions": [], "queue": []}
    write_registry(path, [{"issue": {"id": "PP-1"}}], [{"issue": {"id": "PP-2"}}])
    assert read_registry(path) == {
        "sessions": [{"issue": {"id": "PP-1"}}],
        "queue": [{"issue": {"id": "PP-2"}}],
    }
    path.write_text(json.dumps([{"issue": {"id": "PP-3"}}]))
    assert read_registry(path) == {"sessions": [{"issue": {"id": "PP-3"}}], "queue": []}
    path.write_text("not json")
    assert read_registry(path) == {"sessions": [], "queue": []}


def started_log(manager, monkeypatch, tmp_path):
    log: list[tuple[str, str, bool]] = []

    def fake_start(self, found, mode, resume=False, base="", flags=None, resume_id="", prompt=""):
        log.append((found.id, base, resume))
        add_session(self, tmp_path, found)
        return self._sessions[found.id]

    monkeypatch.setattr(SessionManager, "start", fake_start)
    return log


def test_the_fourth_builder_queues_and_starts_when_a_slot_frees(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, max_builders=3))
    log = started_log(manager, monkeypatch, tmp_path)
    outcomes = [manager.request_start(issue(f"PP-{n}"), "plan") for n in (1, 2, 3, 4, 5)]
    assert outcomes == ["started", "started", "started", "queued", "queued"]
    assert manager.queued_ids() == {"PP-4", "PP-5"}
    assert [e["position"] for e in manager.queue_view()] == [1, 2]
    assert manager.request_start(issue("PP-4"), "plan") == "queued"
    assert manager.drain() == []
    manager.stop("PP-1")
    assert manager.drain() == ["PP-4"]
    assert [entry[0] for entry in log] == ["PP-1", "PP-2", "PP-3", "PP-4"]
    assert manager.queued_ids() == {"PP-5"}


def test_a_builder_at_the_human_phase_does_not_hold_a_slot(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, max_builders=2))
    started_log(manager, monkeypatch, tmp_path)
    add_session(manager, tmp_path, issue("PP-1"), phase="human")
    add_session(manager, tmp_path, issue("PP-2"), phase="build")
    assert [s.issue.id for s in manager.counted()] == ["PP-2"]
    assert manager.request_start(issue("PP-3"), "plan") == "started"
    assert manager.request_start(issue("PP-4"), "plan") == "queued"


def test_cancelling_a_queued_builder_removes_it(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, max_builders=1))
    started_log(manager, monkeypatch, tmp_path)
    manager.request_start(issue("PP-1"), "plan")
    manager.request_start(issue("PP-2"), "plan")
    manager.cancel_queued("PP-2")
    assert manager.queued_ids() == set()
    assert read_registry(manager._settings.state_path)["queue"] == []


def test_the_queue_survives_a_restart_and_restore_respects_the_cap(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, max_builders=2)
    entries = [
        {"issue": asdict(issue(f"PP-{n}")), "mode": "plan", "base": "develop"} for n in (1, 2, 3)
    ]
    queued = [
        {
            "issue": asdict(issue("PP-9")),
            "mode": "auto",
            "base": "feature/pp-1",
            "resume": False,
            "flags": {},
        }
    ]
    write_registry(settings.state_path, entries, queued)
    manager = SessionManager(settings)
    log = started_log(manager, monkeypatch, tmp_path)
    assert manager.restore() == ["PP-1", "PP-2", "PP-3"]
    assert [(i, r) for i, _, r in log] == [("PP-1", True), ("PP-2", True)]
    assert manager.queued_ids() == {"PP-9", "PP-3"}


def test_restore_keeps_blockers_and_the_base_of_a_queued_child(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, max_builders=1)
    child = issue("PP-7", blocked_by=(Blocker("PP-6", "started"),))
    manager = SessionManager(settings)
    started_log(manager, monkeypatch, tmp_path)
    manager.request_start(issue("PP-1"), "plan")
    manager.request_start(child, "plan", base="feature/pp-6")
    saved = read_registry(settings.state_path)["queue"][0]
    assert saved["base"] == "feature/pp-6"
    assert Issue.from_dict(saved["issue"]).blocked_by == (Blocker("PP-6", "started"),)


def test_stop_removes_a_builder_from_the_registry_but_quitting_does_not(tmp_path):
    settings = settings_for(tmp_path)
    manager = SessionManager(settings)
    add_session(manager, tmp_path, issue("PP-1035"))
    manager._persist()
    manager.stop_all()
    assert [e["issue"]["id"] for e in read_registry(settings.state_path)["sessions"]] == ["PP-1035"]
    manager.stop("PP-1035")
    assert read_registry(settings.state_path)["sessions"] == []


def test_base_and_approval_flags_survive_a_restart_through_the_registry(tmp_path):
    settings = settings_for(tmp_path)
    manager = SessionManager(settings)
    session = add_session(manager, tmp_path, issue("PP-1035"))
    session.base, session.base_sha = "feature/pp-1034", "abc123"
    session.label_done, session.slack_link, session.approved = True, "https://x.slack.com/p1", True
    manager._persist()
    entry = read_registry(settings.state_path)["sessions"][0]
    assert (entry["base"], entry["baseSha"]) == ("feature/pp-1034", "abc123")
    assert (entry["labelDone"], entry["slackLink"], entry["approved"]) == (
        True,
        "https://x.slack.com/p1",
        True,
    )


def test_a_session_added_while_the_sessions_are_being_walked_does_not_break_the_walk(tmp_path):
    manager = SessionManager(settings_for(tmp_path))
    first = add_session(manager, tmp_path, issue("ABC-1", team="ABC"))
    add_session(manager, tmp_path, issue("ABC-2", team="ABC"))
    added = []
    original = type(first).health

    def adding_health(self, waiting_after):
        if not added:
            added.append(add_session(manager, tmp_path, issue("ABC-3", team="ABC")))
        return original(self, waiting_after)

    type(first).health = adding_health
    try:
        manager.check_waiting()
        manager.persist() if hasattr(manager, "persist") else None
        manager.release_dead()
    finally:
        type(first).health = original
    assert added and "ABC-3" in manager._sessions


def test_a_builder_with_no_saved_conversation_starts_fresh_instead_of_resuming(tmp_path):
    from cyclecontrol.sessions import resume_choice

    worktree = tmp_path / "spp-1"
    worktree.mkdir()
    assert resume_choice(worktree, True, "") == (False, "", True)
    assert resume_choice(worktree, True, "abc") == (True, "abc", False)
    assert resume_choice(worktree, False, "") == (False, "", False)


def test_a_saved_conversation_is_resumed(tmp_path, monkeypatch):
    from cyclecontrol import sessions

    monkeypatch.setattr(sessions, "newest_transcript", lambda worktree: tmp_path / "x.jsonl")
    assert sessions.resume_choice(tmp_path, True, "") == (True, "", False)


def saved_entries(tmp_path, phases: dict[str, str]) -> list[dict]:
    for issue_id, phase in phases.items():
        gitdir = tmp_path / f"spp-{issue_id.rsplit('-', 1)[-1]}" / ".git"
        gitdir.mkdir(parents=True)
        (gitdir / "spp-phase.json").write_text(json.dumps({"phase": phase}))
    return [{"issue": asdict(issue(i)), "mode": "plan", "base": "develop"} for i in phases]


def test_restore_resumes_working_builders_and_sleeps_the_ones_that_handed_over(
    tmp_path, monkeypatch
):
    settings = settings_for(tmp_path, max_builders=3)
    entries = saved_entries(
        tmp_path, {"PP-1": "human", "PP-2": "execute", "PP-3": "human", "PP-4": "gates"}
    )
    write_registry(settings.state_path, entries, [])
    manager = SessionManager(settings)
    log = started_log(manager, monkeypatch, tmp_path)
    assert manager.restore() == ["PP-2", "PP-4"]
    assert [(i, r) for i, _, r in log] == [("PP-2", True), ("PP-4", True)]
    parked = [s for s in manager.all() if s.asleep]
    assert sorted(s.issue.id for s in parked) == ["PP-1", "PP-3"]
    assert all(s.outcome(20) == "sleeping" and not s.pty.is_alive() for s in parked)
    assert manager.queued_ids() == set()
    assert sorted(e["issue"]["id"] for e in read_registry(settings.state_path)["sessions"]) == [
        "PP-1",
        "PP-2",
        "PP-3",
        "PP-4",
    ]


def test_resume_all_on_launch_resumes_every_saved_builder(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, resume_all_on_launch=True)
    write_registry(settings.state_path, saved_entries(tmp_path, {"PP-1": "human"}), [])
    manager = SessionManager(settings)
    log = started_log(manager, monkeypatch, tmp_path)
    assert manager.restore() == ["PP-1"] and [i for i, _, _ in log] == ["PP-1"]


def test_live_claude_processes_are_capped_whatever_their_phase(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, max_builders=3, max_live_builders=3))
    started_log(manager, monkeypatch, tmp_path)
    add_session(manager, tmp_path, issue("PP-1"), phase="human")
    add_session(manager, tmp_path, issue("PP-2"), phase="human")
    add_session(manager, tmp_path, issue("PP-3"), phase="human")
    assert manager.counted() == [] and len(manager.live()) == 3
    assert manager.request_start(issue("PP-4"), "plan") == "queued"
    manager.stop("PP-1")
    assert manager.drain() == ["PP-4"]


def test_an_idle_builder_at_the_human_phase_is_put_to_sleep_and_wake_resumes_its_conversation(
    tmp_path, monkeypatch
):
    manager = SessionManager(settings_for(tmp_path, park_after_seconds=600))
    handed = add_session(manager, tmp_path, issue("PP-1"), phase="human")
    working = add_session(manager, tmp_path, issue("PP-2"), phase="execute")
    handed.pty.last_output = working.pty.last_output = 1000.0
    handed.label_done = True
    old_pty = handed.pty
    assert manager.sleep_idle(now=1500.0) == []
    assert [s.issue.id for s in manager.sleep_idle(now=1700.0)] == ["PP-1"]
    assert handed.asleep and old_pty.terminated and not handed.pty.is_alive()
    assert handed.outcome(20) == "sleeping" and working.pty.is_alive()
    assert manager.sleep_idle(now=1800.0) == []
    log = started_log(manager, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "cyclecontrol.sessions.newest_transcript", lambda w, home=None: tmp_path / "abc.jsonl"
    )
    assert manager.wake("PP-1") == "started"
    assert log == [("PP-1", "", True)]
    assert manager.wake("PP-1") == "running" and manager.wake("PP-9") == "unknown"


def test_a_builder_mid_run_at_the_human_phase_is_not_put_to_sleep(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, park_after_seconds=600))
    busy = add_session(manager, tmp_path, issue("PP-1"), phase="human")
    busy.pty.last_output = 1000.0
    busy.pty.pid = 77
    monkeypatch.setattr("cyclecontrol.sessions.WATCHER.running", lambda pid: pid == 77)
    assert manager.sleep_idle(now=5000.0) == []
    off = SessionManager(settings_for(tmp_path, park_after_seconds=0))
    add_session(off, tmp_path, issue("PP-2"), phase="human").pty.last_output = 0.0
    assert off.sleep_idle(now=5000.0) == []


def test_progress_is_re_read_only_when_a_progress_file_changes(tmp_path):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("PP-1"), phase="plan")
    assert session.progress().phase == "plan"
    (session.git_dir / "spp-phase.json").write_text(json.dumps({"phase": "human"}))
    assert session.progress().phase == "human"
    assert session.progress() is session.progress()


def test_a_builder_waiting_on_you_for_an_hour_is_put_to_sleep_but_not_mid_work(
    tmp_path, monkeypatch
):
    manager = SessionManager(settings_for(tmp_path, waiting_close_seconds=3600))
    waiting = add_session(manager, tmp_path, issue("PP-1"), phase="execute")
    conflicted = add_session(manager, tmp_path, issue("PP-2"), phase="execute")
    busy = add_session(manager, tmp_path, issue("PP-3"), phase="execute")
    for s in (waiting, conflicted, busy):
        s.pty.last_output = 1000.0
    conflicted.attention = "Rebase conflict onto develop"
    busy.pty.pid = 77
    monkeypatch.setattr("cyclecontrol.sessions.WATCHER.running", lambda pid: pid == 77)
    assert manager.sleep_idle(now=4000.0) == []
    assert [s.issue.id for s in manager.sleep_idle(now=4700.0)] == ["PP-1"]
    assert waiting.outcome(20) == "sleeping" and waiting._reader is None
    entries = {e["issue"]["id"]: e for e in read_registry(manager._settings.state_path)["sessions"]}
    assert entries["PP-1"]["asleep"] is True and entries["PP-2"]["asleep"] is False


def test_finished_builders_leave_the_panel_two_weeks_after_their_pr_closed(tmp_path):
    """2026-11-10: 78 finished builders sat in the side panel for good, closed and merged alike."""
    from cyclecontrol.sessions import FORGET_FINISHED_AFTER_SECONDS

    manager = SessionManager(settings_for(tmp_path))
    fresh = add_session(manager, tmp_path, issue("PP-1", "ABC"))
    stale = add_session(manager, tmp_path, issue("PP-2", "ABC"))
    live = add_session(manager, tmp_path, issue("PP-3", "ABC"))
    for session in (fresh, stale):
        session.pty.write = lambda data: None
        manager.finish(session.issue.id)
    assert fresh.finished_at > 0
    stale.finished_at = time.time() - FORGET_FINISHED_AFTER_SECONDS - 60
    assert manager.forget_finished() == ["PP-2"]
    assert [s.issue.id for s in manager.all()] == ["PP-1", "PP-3"]
    assert live.finished is False
    saved = read_registry(manager._settings.state_path)["sessions"]
    assert [e["issue"]["id"] for e in saved] == ["PP-1", "PP-3"]
    assert saved[0]["finishedAt"] == fresh.finished_at


def test_a_finished_builder_saved_before_timestamps_counts_from_the_restart(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, max_builders=5, max_live_builders=5)
    entries = saved_entries(tmp_path, {"PP-4": "execute"})
    entries[0]["finished"] = True
    write_registry(settings.state_path, entries, [])
    manager = SessionManager(settings)
    started_log(manager, monkeypatch, tmp_path)
    manager.restore()
    session = manager.get("PP-4")
    assert session.finished and time.time() - session.finished_at < 5
    assert manager.forget_finished() == []


def test_restore_wakes_only_builders_that_were_working(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, max_builders=5, max_live_builders=5)
    entries = saved_entries(
        tmp_path, {"PP-1": "execute", "PP-2": "execute", "PP-3": "execute", "PP-4": "execute"}
    )
    entries[1]["waiting"] = True
    entries[2]["asleep"] = True
    entries[3]["finished"] = True
    write_registry(settings.state_path, entries, [])
    manager = SessionManager(settings)
    log = started_log(manager, monkeypatch, tmp_path)
    assert manager.restore() == ["PP-1"] and [i for i, _, _ in log] == ["PP-1"]
    outcomes = {s.issue.id: s.outcome(20) for s in manager.all() if not s.pty.is_alive()}
    assert outcomes == {"PP-2": "sleeping", "PP-3": "sleeping", "PP-4": "finished"}
    assert manager.live() == [manager.get("PP-1")]


def test_wake_carries_the_message_as_the_resume_prompt_and_a_running_builder_just_gets_it(
    tmp_path, monkeypatch
):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("PP-1"), phase="execute")
    sent = []
    session.pty.write = sent.append
    monkeypatch.setattr(
        "cyclecontrol.sessions.threading.Timer",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    assert manager.wake("PP-1", "carry on") == "running" and sent == [b"carry on"]
    manager._sleep(session)
    prompts = []

    def fake_start(self, found, mode, resume=False, base="", flags=None, resume_id="", prompt=""):
        prompts.append((found.id, resume, resume_id, prompt, flags))
        return add_session(self, tmp_path, found)

    monkeypatch.setattr(SessionManager, "start", fake_start)
    monkeypatch.setattr(
        "cyclecontrol.sessions.newest_transcript", lambda w, home=None: tmp_path / "abc.jsonl"
    )
    assert manager.wake("PP-1", "carry on") == "started"
    assert prompts == [("PP-1", True, "abc", "carry on", flags_of(session))]


def test_a_waiting_flip_is_written_to_the_registry_so_launch_knows(tmp_path):
    settings = settings_for(tmp_path, waiting_after_seconds=20)
    manager = SessionManager(settings)
    session = add_session(manager, tmp_path, issue("PP-1"), phase="execute")
    session.pty.last_output = time.time()
    manager.check_waiting()
    assert read_registry(settings.state_path)["sessions"] == []
    session.pty.last_output = 0.0
    manager.check_waiting()
    assert read_registry(settings.state_path)["sessions"][0]["waiting"] is True


def test_stop_on_a_sleeping_builder_still_removes_it(tmp_path):
    settings = settings_for(tmp_path)
    manager = SessionManager(settings)
    session = add_session(manager, tmp_path, issue("PP-1"), phase="human")
    manager._sleep(session)
    manager.stop("PP-1")
    assert session.stopped and read_registry(settings.state_path)["sessions"] == []


def test_a_builder_waiting_on_ci_sleeps_even_with_a_background_shell(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, park_after_seconds=600))
    waiting = add_session(manager, tmp_path, issue("PP-1"), phase="ci")
    waiting.pty.last_output = 1000.0
    waiting.pty.pid = 77
    monkeypatch.setattr("cyclecontrol.sessions.WATCHER.running", lambda pid: pid == 77)
    assert [s.issue.id for s in manager.sleep_idle(now=1700.0)] == ["PP-1"]


class ChattyPty(Pty):
    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def tail(self) -> bytes:
        return self.text.encode()


def test_every_pr_the_builder_printed_is_listed_once_in_order(tmp_path):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("SLM-1202", "ABC"), phase="human")
    session.pty = ChattyPty(
        "Backend: https://github.com/o/abc/pull/35116\n"
        "Frontend, stacked: https://github.com/o/abc/pull/35122\n"
        "Backend again: https://github.com/o/abc/pull/35116\n"
    )
    assert session.pr_urls_from_chat() == [
        "https://github.com/o/abc/pull/35116",
        "https://github.com/o/abc/pull/35122",
    ]
    assert session.pr_url_from_chat() == "https://github.com/o/abc/pull/35122"
    assert session.pr_urls() == session.pr_urls_from_chat()


def test_approving_a_stack_labels_and_posts_every_pr_and_skips_what_was_already_done(
    tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from cyclecontrol import server
    from cyclecontrol.github import PullRequest

    settings = settings_for(tmp_path, comments_file=str(tmp_path / "c.json"))
    app = server.App(settings)
    session = add_session(app.sessions, tmp_path, issue("SLM-1202", "ABC"), phase="human")
    session.pty = ChattyPty(
        "Backend: https://github.com/o/abc/pull/35116\n"
        "Frontend: https://github.com/o/abc/pull/35122\n"
    )
    session.label_done, session.slack_link = True, "https://x.slack.com/front"
    labelled, posted = [], []
    monkeypatch.setattr(
        server,
        "get_pr",
        lambda repo, n: PullRequest(n, f"PR {n}", f"https://github.com/{repo}/pull/{n}", False, []),
    )
    monkeypatch.setattr(server, "add_label", lambda repo, n, label: labelled.append((n, label)))
    monkeypatch.setattr(
        server,
        "post_pr_link",
        lambda *a: posted.append(a[2]) or f"https://x.slack.com/{a[2]}",
    )

    request = SimpleNamespace(match_info={"issue_id": "SLM-1202"})

    async def run():
        first = await app.post_approve(request)
        second = await app.post_approve(request)
        return first.status, second.status

    assert asyncio.run(run()) == (200, 200)
    assert labelled == [(35116, settings.merge_label)]
    assert posted == ["PR 35116"]
    assert session.slack_links == {
        "https://github.com/o/abc/pull/35122": "https://x.slack.com/front",
        "https://github.com/o/abc/pull/35116": "https://x.slack.com/PR 35116",
    }
    assert session.approved and read_registry(settings.state_path)["sessions"][0]["slackLinks"]


def test_approving_a_pr_in_a_repo_without_a_merge_queue_skips_the_label(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from cyclecontrol import server
    from cyclecontrol.github import PullRequest

    settings = settings_for(tmp_path, comments_file=str(tmp_path / "c.json"))
    app = server.App(settings)
    session = add_session(app.sessions, tmp_path, issue("SLM-1059", "ABC"), phase="human")
    session.pty = ChattyPty("https://github.com/AgentSoftware/street-infra/pull/503\n")
    labelled, posted = [], []
    monkeypatch.setattr(
        server,
        "get_pr",
        lambda repo, n: PullRequest(n, f"PR {n}", f"https://github.com/{repo}/pull/{n}", False, []),
    )
    monkeypatch.setattr(server, "add_label", lambda repo, n, label: labelled.append((n, label)))
    monkeypatch.setattr(
        server, "post_pr_link", lambda *a: posted.append(a[2]) or "https://x.slack.com/p"
    )
    request = SimpleNamespace(match_info={"issue_id": "SLM-1059"})
    response = asyncio.run(app.post_approve(request))
    assert response.status == 200
    assert labelled == [], "street-infra has no mergequeue label, so gh would refuse it"
    assert posted == ["PR 503"] and session.approved


def test_a_builder_cut_off_by_an_api_error_is_told_to_carry_on_once(tmp_path):
    from cyclecontrol.sessions import CUT_OFF_NOTE

    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("SLM-1271", "ABC"), phase="human")
    screen = (
        "Update(app/Concerns/HasAccessSnapshot.php)\n"
        "API Error: Your computer went to sleep mid-response. The response above may be incomplete.\n"
        "* Churned for 3h 0m 39s\n> \n[Sonnet 5.5] slm-1271 -- INSERT --\n"
    )
    session.pty = ChattyPty(screen)
    session.pty.last_output = 1000.0
    sent = []
    session.pty.write = lambda data: sent.append(data)

    assert manager.nudge_cut_off(now=1005.0) == [], "give the builder a moment first"
    assert [s.issue.id for s in manager.nudge_cut_off(now=1030.0)] == ["SLM-1271"]
    assert sent and sent[0] == CUT_OFF_NOTE.encode()
    assert manager.nudge_cut_off(now=1090.0) == [], "same stall, not nudged again"

    session.pty.last_output = 2000.0
    assert [s.issue.id for s in manager.nudge_cut_off(now=2030.0)] == ["SLM-1271"], "a new stall"
    assert session.nudges == 2


def test_a_cut_off_drawn_with_cursor_moves_instead_of_spaces_is_still_a_stall(tmp_path):
    """SLM-1275, 2026-11-06: Claude Code drew the error with cursor-forward codes, so stripped text read
    "wenttosleepmid-response" and the builder sat at the prompt all night."""
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("SLM-1275", "ABC"))
    screen = (
        "\x1b[38;2;255;107;107mAPI\x1b[1CError:\x1b[1CYour\x1b[1Ccomputer\x1b[1Cwent\x1b[1Cto\x1b[1Csleep"
        "\x1b[1Cmid-response.\x1b[1CThe\x1b[1Cresponse\x1b[1Cabove\x1b[1Cmay\x1b[1Cbe\x1b[1Cincomplete.\x1b[39m\r\r\n"
        "\u273b\x1b[1CSaut\u00e9ed\x1b[1Cfor\x1b[1C3h\x1b[1C7m\x1b[1C6s\r\r\n\u276f\x1b[3C\r\r\n"
    )
    session.pty = ChattyPty(screen)
    session.pty.last_output = 1000.0
    session.pty.write = lambda data: None

    assert [s.issue.id for s in manager.nudge_cut_off(now=1030.0)] == ["SLM-1275"]


def test_an_old_api_error_further_up_the_screen_is_not_a_stall_and_the_log_says_why(
    tmp_path, caplog
):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("SLM-1", "ABC"))
    session.pty = ChattyPty("API Error: Request timed out.\n" + ("Working on it...\n" * 200))
    session.pty.last_output = 10.0
    with caplog.at_level("INFO"):
        assert manager.nudge_cut_off(now=100.0) == []
        assert manager.nudge_cut_off(now=130.0) == []
    assert caplog.text.count("SLM-1 shows an API error but was not nudged") == 1
    assert "characters above the end of the screen" in caplog.text


def test_a_gh_timeout_is_a_github_error(monkeypatch):
    import subprocess

    from cyclecontrol import github

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 60)

    monkeypatch.setattr(github.subprocess, "run", slow)
    with pytest.raises(github.GithubError, match="timed out"):
        github._gh("api", "graphql")


def test_a_builder_that_quit_on_its_own_can_be_woken_like_a_sleeping_one(tmp_path):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("SLM-1061", "ABC"), phase="human", alive=False)
    session.pty.exit_status = lambda: 0
    assert session.outcome(20) == "ended", "Ctrl-C twice, not parked by the app and not finished"
    session.pty.exit_status = lambda: 1
    assert session.outcome(20) == "failed"
    session.stopped = True
    assert session.outcome(20) == "done"


def test_a_pr_elliot_posted_in_slack_himself_is_recorded_so_approve_skips_the_post(
    tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from cyclecontrol import server
    from cyclecontrol.github import PullRequest

    settings = settings_for(tmp_path, comments_file=str(tmp_path / "c.json"))
    app = server.App(settings)
    session = add_session(app.sessions, tmp_path, issue("SLM-1328", "ABC"), phase="human")
    session.pty = ChattyPty("https://github.com/o/abc/pull/1760\n")
    assert app.sessions.unposted_pr_urls() == {"SLM-1328": ["https://github.com/o/abc/pull/1760"]}

    marked = app.sessions.mark_posted(
        {"https://github.com/o/abc/pull/1760": "https://x.slack.com/archives/C1/p1"}
    )
    assert [(s.issue.id, url) for s, url, _ in marked] == [
        ("SLM-1328", "https://github.com/o/abc/pull/1760")
    ]
    assert session.slack_link == "https://x.slack.com/archives/C1/p1" and not session.approved
    assert app.sessions.unposted_pr_urls() == {}, "nothing left to look for"

    labelled, posted = [], []
    monkeypatch.setattr(
        server,
        "get_pr",
        lambda repo, n: PullRequest(n, f"PR {n}", f"https://github.com/{repo}/pull/{n}", False, []),
    )
    monkeypatch.setattr(server, "add_label", lambda repo, n, label: labelled.append(n))
    monkeypatch.setattr(server, "post_pr_link", lambda *a: posted.append(a[2]))
    request = SimpleNamespace(match_info={"issue_id": "SLM-1328"})
    assert asyncio.run(app.post_approve(request)).status == 200
    assert labelled == [1760] and posted == [], "the label goes on, the Slack post is not repeated"
    assert session.approved


def test_builder_limits_can_be_raised_while_the_app_runs(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from cyclecontrol import server

    settings = settings_for(tmp_path, comments_file=str(tmp_path / "c.json"))
    app = server.App(settings)
    saved = []
    monkeypatch.setattr(server, "save_env_value", lambda key, value: saved.append((key, value)))

    async def body():
        return {"maxLiveBuilders": "10", "maxBuilders": 8}

    response = asyncio.run(app.post_settings(SimpleNamespace(json=body)))

    assert response.status == 200
    assert (settings.max_live_builders, settings.max_builders) == (10, 8)
    assert sorted(saved) == [("MAX_BUILDERS", "8"), ("MAX_LIVE_BUILDERS", "10")]

    async def bad():
        return {"maxLiveBuilders": "lots"}

    assert asyncio.run(app.post_settings(SimpleNamespace(json=bad))).status == 400


def test_approving_a_stacked_pr_walks_down_to_its_base_pr(tmp_path, monkeypatch):
    """SLM-1428: only #1838 was known, and it targeted #1837's branch. Both get the label."""
    from cyclecontrol import server
    from cyclecontrol.github import GithubError, PullRequest

    def pr(number, base):
        return PullRequest(
            number, f"PR {number}", f"https://github.com/o/abc/pull/{number}", False, [], base
        )

    opened = {"feature/slm-1428": pr(1838, "feature/editor"), "feature/editor": pr(1837, "develop")}

    def find_open(repo, branch):
        if branch not in opened:
            raise GithubError("none")
        return opened[branch]

    monkeypatch.setattr(server, "find_open_pr", find_open)
    settings = settings_for(tmp_path)
    app = server.App(settings)
    session = add_session(app.sessions, tmp_path, issue("SLM-1428", "ABC"), phase="ci")
    session.pty = ChattyPty("no links here\n")

    numbers = [p.number for _, p in app._prs_to_approve(session, "o/abc")]

    assert numbers == [1837, 1838]


def test_a_builder_idle_in_ci_with_green_checks_is_told_to_hand_over(tmp_path, monkeypatch):
    from cyclecontrol import server

    app = server.App(settings_for(tmp_path, comments_file=str(tmp_path / "c.json")))
    session = add_session(app.sessions, tmp_path, issue("SLM-1428", "ABC"), phase="ci")
    session.pty = ChattyPty("waiting on CircleCI\n")
    session.pty.last_output = 1000.0
    sent = []
    session.pty.write = lambda data: sent.append(data.decode())
    view = {"hasPr": True, "prNumber": 1838, "status": {"open": True, "ci": "SUCCESS"}}
    monkeypatch.setattr(app.comments, "view", lambda s: view)

    assert app.nudge_green_ci(now=1100.0) == [], "ten minutes first"
    assert [s.issue.id for s in app.nudge_green_ci(now=1700.0)] == ["SLM-1428"]
    assert "#1838" in sent[0] and "phase.sh human" in sent[0]
    assert app.nudge_green_ci(now=2400.0) == [], "once per stall"

    view["status"]["ci"] = "FAILURE"
    session.pty.last_output = 3000.0
    assert app.nudge_green_ci(now=3700.0) == [], "red CI is the builder's own loop to notice"


def test_an_issue_assigned_to_someone_else_is_not_started_unless_forced(tmp_path, monkeypatch):
    manager = SessionManager(settings_for(tmp_path, max_builders=3))
    log = started_log(manager, monkeypatch, tmp_path)
    theirs = Issue(
        "PP-9",
        "Dates",
        "https://x/PP-9",
        "feature/pp-9",
        "Todo",
        "unstarted",
        "Matt",
        False,
        False,
        team_key="PP",
    )
    with pytest.raises(NotYoursError):
        manager.request_start(theirs, "plan")
    assert log == []
    assert manager.request_start(theirs, "plan", force=True) == "started"
    assert [entry[0] for entry in log] == ["PP-9"]
    unassigned = Issue(
        "PP-10",
        "Dates",
        "https://x/PP-10",
        "feature/pp-10",
        "Todo",
        "unstarted",
        "",
        False,
        False,
        team_key="PP",
    )
    assert manager.request_start(unassigned, "plan") == "started"


def test_approve_skips_a_mistyped_pr_link_the_builder_printed(tmp_path, monkeypatch):
    from cyclecontrol import server
    from cyclecontrol.github import GithubError, PullRequest

    def get_pr(repo, number):
        if repo != "o/abc":
            raise GithubError("Could not resolve to a Repository")
        return PullRequest(
            number, f"PR {number}", f"https://github.com/{repo}/pull/{number}", False, []
        )

    monkeypatch.setattr(server, "get_pr", get_pr)
    app = server.App(settings_for(tmp_path))
    session = add_session(app.sessions, tmp_path, issue("SLM-1", "ABC"), phase="human")
    session.pty = ChattyPty(
        "https://github.com/o/abc/pull/7\nhttps://github.com/o/cosumer/pull/7\n"
    )

    assert [p.number for _, p in app._prs_to_approve(session, "o/abc")] == [7]


def test_a_pr_link_the_terminal_wrapped_mid_number_is_not_read_as_a_second_pr():
    from types import SimpleNamespace

    from cyclecontrol import sessions

    base = "https://github.com/AgentSoftware/street/pull/"
    fake = SimpleNamespace(
        pty=SimpleNamespace(tail=lambda: f"{base}36\n{base}36269 and {base}41".encode())
    )
    assert sessions.Session.pr_urls_from_chat(fake) == [f"{base}36269", f"{base}41"]


def test_restore_retries_a_locked_worktree_then_resumes(tmp_path, monkeypatch):
    from cyclecontrol import locks
    from cyclecontrol import sessions as sessions_module

    settings = settings_for(tmp_path, max_builders=3)
    write_registry(settings.state_path, saved_entries(tmp_path, {"PP-2": "execute"}), [])
    manager = SessionManager(settings)
    monkeypatch.setattr(sessions_module, "RESTORE_LOCK_RETRY_SECONDS", 0)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise locks.BuilderLockedError("held by an old process")
        return "started"

    monkeypatch.setattr(manager, "request_start", flaky)
    assert manager.restore() == ["PP-2"] and len(calls) == 3


def test_restore_keeps_a_builder_that_stays_locked_instead_of_dropping_it(tmp_path, monkeypatch):
    from cyclecontrol import locks
    from cyclecontrol import sessions as sessions_module

    settings = settings_for(tmp_path, max_builders=3)
    write_registry(settings.state_path, saved_entries(tmp_path, {"PP-2": "execute"}), [])
    manager = SessionManager(settings)
    monkeypatch.setattr(sessions_module, "RESTORE_LOCK_WAIT_SECONDS", 0)

    def locked(*args, **kwargs):
        raise locks.BuilderLockedError("held")

    monkeypatch.setattr(manager, "request_start", locked)
    assert manager.restore() == []
    kept = manager.get("PP-2")
    assert kept is not None and kept.asleep
