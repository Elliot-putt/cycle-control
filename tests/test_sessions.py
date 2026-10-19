import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from cyclecontrol import worktrees
from cyclecontrol.config import Settings, load_teams
from cyclecontrol.models import Blocker, Issue
from cyclecontrol.sessions import (
    Session,
    SessionManager,
    UnmappedTeamError,
    builder_argv,
    builder_prompt,
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
