import json
import subprocess
from datetime import UTC, datetime

import pytest

from cyclecontrol.config import Settings, save_env_value
from cyclecontrol.linear import parse_dashboard
from cyclecontrol.models import Issue
from cyclecontrol.progress import read_progress
from cyclecontrol.pty import latest_line
from cyclecontrol.sessions import health_of


def node(identifier: str, state_type: str = "unstarted", name: str = "Todo") -> dict:
    return {
        "identifier": identifier,
        "title": f"Title {identifier}",
        "url": f"https://linear.app/x/issue/{identifier}",
        "branchName": f"feature/{identifier.lower()}",
        "state": {"name": name, "type": state_type},
        "assignee": {"id": "u1", "name": "Elliot"},
    }


def dashboard() -> dict:
    return {
        "viewer": {"assignedIssues": {"nodes": [node("PP-1"), node("PP-9")]}},
        "cycles": {
            "nodes": [
                {
                    "number": 42,
                    "endsAt": "2026-10-24T09:00:00Z",
                    "issues": {"nodes": [node("PP-1"), node("PP-2", "completed", "Done")]},
                }
            ]
        },
    }


def test_parse_dashboard_marks_cycle_mine_and_off_cycle():
    now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    cycle, issues = parse_dashboard(dashboard(), now)
    assert (cycle.number, cycle.days_left, cycle.done, cycle.total) == (42, 5, 1, 2)
    by_id = {i.id: i for i in issues}
    assert by_id["PP-1"].in_cycle and by_id["PP-1"].is_mine
    assert by_id["PP-2"].in_cycle and not by_id["PP-2"].is_mine
    assert not by_id["PP-9"].in_cycle and by_id["PP-9"].is_mine
    assert len(issues) == 3


def test_parse_dashboard_without_active_cycle_keeps_assigned_issues():
    data = dashboard()
    data["cycles"]["nodes"] = []
    cycle, issues = parse_dashboard(data)
    assert cycle is None
    assert [i.id for i in issues] == ["PP-1", "PP-9"]
    assert not any(i.in_cycle for i in issues)


@pytest.mark.parametrize(
    ("alive", "phase", "idle", "expected"),
    [
        (False, "execute", 0, "done"),
        (True, "human", 0, "waiting"),
        (True, "plan", 5, "working"),
        (True, "plan", 30, "waiting"),
    ],
)
def test_health_of(alive, phase, idle, expected):
    assert health_of(alive, phase, idle, 20) == expected


def issue() -> Issue:
    return Issue(
        "PP-1035",
        "Dates",
        "https://x/PP-1035",
        "feature/pp-1035",
        "Todo",
        "unstarted",
        "E",
        False,
        True,
    )


def test_read_progress(tmp_path):
    (tmp_path / "spp-phase.json").write_text(
        json.dumps({"phase": "human", "note": "https://github.com/o/r/pull/1"})
    )
    (tmp_path / "spp-plan.json").write_text(
        json.dumps({"summary": "S", "steps": [{"id": "s1", "label": "L", "kind": "backend"}]})
    )
    (tmp_path / "spp-gates.json").write_text(json.dumps({"ok": True}))
    (tmp_path / "spp-review.json").write_text(json.dumps({"verdict": "CHANGES"}))
    progress = read_progress(tmp_path)
    assert progress.phase == "human" and progress.pr_url.endswith("/pull/1")
    assert progress.plan[0].label == "L"
    assert (progress.gates, progress.review) == ("pass", "CHANGES")


def test_read_progress_when_nothing_written(tmp_path):
    progress = read_progress(tmp_path)
    assert (progress.phase, progress.gates, progress.review, progress.plan) == (
        "starting",
        "none",
        "none",
        [],
    )


def test_latest_line_strips_terminal_codes():
    raw = b"\x1b[2J\x1b[1;32mWriting the plan\x1b[0m\r\n\x1b[?25l  \r\n"
    assert latest_line(raw) == "Writing the plan"


def test_save_env_value_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# note\nPORT=1\nLINEAR_API_KEY=\n")
    save_env_value("LINEAR_API_KEY", "abc", env)
    save_env_value("NEW", "1", env)
    assert env.read_text() == "# note\nPORT=1\nLINEAR_API_KEY=abc\nNEW=1\n"


def waiting_manager(tmp_path, monkeypatch):
    from cyclecontrol import sessions as sessions_module
    from cyclecontrol.sessions import Session, SessionManager

    class LivePty:
        last_output = 0.0

        def is_alive(self):
            return True

    monkeypatch.setattr(sessions_module.time, "time", lambda: 1000.0)
    manager = SessionManager(
        Settings(state_file=str(tmp_path / "s.json"), waiting_after_seconds=20)
    )
    session = Session(issue(), "plan", tmp_path, tmp_path, LivePty())
    manager._sessions["PP-1035"] = session
    return manager, session


def test_waiting_builder_is_announced_once_then_reminded_once_after_five_minutes(
    tmp_path, monkeypatch
):
    manager, session = waiting_manager(tmp_path, monkeypatch)
    fresh, reminders = manager.check_waiting(now=1000.0)
    assert (fresh, reminders) == ([session], [])
    assert manager.check_waiting(now=1100.0) == ([], [])
    assert manager.check_waiting(now=1299.0) == ([], [])
    assert manager.check_waiting(now=1300.0) == ([], [session])
    assert manager.check_waiting(now=1900.0) == ([], [])
    assert manager.check_waiting(now=5000.0) == ([], [])


def test_a_builder_that_works_again_gets_a_new_announcement_and_reminder(tmp_path, monkeypatch):
    manager, session = waiting_manager(tmp_path, monkeypatch)
    manager.check_waiting(now=1000.0)
    manager.check_waiting(now=1300.0)
    session.pty.last_output = 999.0
    assert manager.check_waiting(now=1301.0) == ([], [])
    assert (session.was_waiting, session.reminded, session.waiting_since) == (False, False, None)
    session.pty.last_output = 0.0
    assert manager.check_waiting(now=1400.0) == ([session], [])
    assert manager.check_waiting(now=1700.0) == ([], [session])


def test_a_resumed_builder_is_not_announced_but_is_reminded(tmp_path, monkeypatch):
    manager, session = waiting_manager(tmp_path, monkeypatch)
    session.was_waiting = True
    assert manager.check_waiting(now=1000.0) == ([], [])
    assert manager.check_waiting(now=1300.0) == ([], [session])


def test_slack_message_is_the_title_as_a_link_with_brackets_escaped():
    from cyclecontrol.slack import link_message, prompt_for

    message = link_message(
        "[NOTICKET] Say that app-tier provisioning", "https://github.com/o/r/pull/1"
    )
    assert (
        message == "[\\[NOTICKET\\] Say that app-tier provisioning](https://github.com/o/r/pull/1)"
    )
    assert message in prompt_for("C1", message, "mcp__claude_ai_Slack__slack_send_message")


def test_post_pr_link_returns_the_link_from_claudes_reply(monkeypatch):
    from cyclecontrol import slack

    calls = {}

    def fake_run(argv, **kwargs):
        calls.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(
            argv, 0, "https://sporthub.slack.com/archives/C1/p123\n", ""
        )

    monkeypatch.setattr(slack.subprocess, "run", fake_run)
    link = slack.post_pr_link("claude", "C1", "Title", "https://github.com/o/r/pull/1")
    assert link == "https://sporthub.slack.com/archives/C1/p123"
    assert (
        "mcp__claude_ai_Slack__slack_send_message,mcp__plugin_slack_slack__slack_send_message,mcp__plugin_engineering_slack__slack_send_message,ToolSearch"
        in calls["argv"]
    )
    assert "Title" in calls["input"]


def test_post_pr_link_fails_without_a_link(monkeypatch):
    from cyclecontrol import slack

    monkeypatch.setattr(
        slack.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, "I could not do that", ""),
    )
    with pytest.raises(slack.SlackError):
        slack.post_pr_link("claude", "C1", "T", "https://x")


def test_find_open_pr_and_add_label_use_gh(monkeypatch):
    from cyclecontrol import github

    seen = []

    def fake_gh(*args):
        seen.append(args)
        return json.dumps(
            [
                {
                    "number": 7,
                    "title": "T",
                    "url": "https://github.com/o/r/pull/7",
                    "isDraft": False,
                    "labels": [{"name": "x"}],
                }
            ]
        )

    monkeypatch.setattr(github, "_gh", fake_gh)
    pr = github.find_open_pr("o/r", "feature/pp-1")
    assert (pr.number, pr.labels, pr.is_draft) == (7, ["x"], False)
    assert seen[0][:6] == ("pr", "list", "--repo", "o/r", "--head", "feature/pp-1")
    github.add_label("o/r", 7, "mergequeue")
    assert seen[1] == ("pr", "edit", "7", "--repo", "o/r", "--add-label", "mergequeue")

    monkeypatch.setattr(github, "_gh", lambda *a: "[]")
    with pytest.raises(github.GithubError):
        github.find_open_pr("o/r", "none")


def test_static_assets_are_served_with_no_cache_so_a_reload_gets_the_new_ui(tmp_path):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from cyclecontrol.server import App

    async def run():
        app = App(
            Settings(
                demo=True,
                state_file=str(tmp_path / "s.json"),
                comments_file=str(tmp_path / "c.json"),
            )
        )
        async with TestClient(TestServer(app.build())) as client:
            js = await client.get("/static/app.js")
            api = await client.get("/api/state")
            return js.status, js.headers.get("Cache-Control"), api.headers.get("Cache-Control")

    assert asyncio.run(run()) == (200, "no-cache", None)


def test_the_screen_and_thread_dump_endpoints_answer_without_a_builder(tmp_path):
    """SLM-1438, 2026-11-10: a builder sat on an API error all night with no nudge and the UI crawled; neither
    could be looked into from outside the process."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from cyclecontrol.server import App

    async def run():
        app = App(
            Settings(
                demo=True,
                state_file=str(tmp_path / "s.json"),
                comments_file=str(tmp_path / "c.json"),
            )
        )
        async with TestClient(TestServer(app.build())) as client:
            missing = await client.get("/api/sessions/SLM-0/screen")
            threads = await (await client.get("/api/debug/threads")).json()
            return missing.status, [t["name"] for t in threads["threads"]], threads["threads"][0]

    status, names, first = asyncio.run(run())
    assert status == 404
    assert "MainThread" in names
    assert "File " in first["stack"]


def test_a_background_step_that_fails_is_logged_and_the_loop_carries_on(caplog):
    import asyncio

    from cyclecontrol.server import run_forever

    calls = []

    async def step():
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("head")

    async def run():
        task = asyncio.create_task(run_forever("watch", step, 0.01))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(calls) > 2
    assert "Background step watch failed" in caplog.text


def test_old_saved_pr_state_without_newer_fields_does_not_raise():
    from cyclecontrol.comments import (
        approval_line,
        is_ready_to_merge,
        is_requeue_candidate,
        lane_label,
    )

    old = {
        "approved": True,
        "approvers": ["rio"],
        "ci": "SUCCESS",
        "unresolved": 0,
        "labels": ["blocked"],
    }
    assert is_requeue_candidate(old) is False and is_ready_to_merge(old) is False
    assert lane_label(old, 0, None) == "" and "approved" in approval_line(old, [])
    assert approval_line({"approved": False}, []).startswith("Approval: not approved")


def test_tail_joins_only_the_last_chunks_of_scrollback():
    from cyclecontrol.pty import PtySession, SleepingPty

    pty = PtySession(["true"], "/")
    pty.scrollback = [b"a" * 100, b"b" * 100, b"c" * 100]
    assert pty.tail(150) == b"b" * 50 + b"c" * 100
    assert pty.tail(1000) == b"a" * 100 + b"b" * 100 + b"c" * 100
    asleep = SleepingPty()
    assert asleep.tail() == b"" and asleep.attach(lambda _: None) == []
    assert not asleep.is_alive() and asleep.exit_status() is None and asleep.pid == 0


def test_a_clock_jump_past_the_pause_runs_the_catch_up_before_the_next_step():
    import asyncio

    from cyclecontrol.server import CLOCK_JUMP_SECONDS, run_forever

    clock = {"now": 1000.0}
    seen = []

    async def step():
        seen.append("step")

    async def catch_up(away):
        seen.append(("jump", int(away)))

    async def run():
        task = asyncio.create_task(
            run_forever("watch", step, 0.01, after_jump=catch_up, clock=lambda: clock["now"])
        )
        await asyncio.sleep(0.05)
        clock["now"] += CLOCK_JUMP_SECONDS + 600
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    jumps = [s for s in seen if s != "step"]
    assert len(jumps) == 1 and jumps[0][1] >= CLOCK_JUMP_SECONDS + 600 - 1
    assert seen[seen.index(jumps[0]) + 1] == "step"


def test_posted_pr_links_reads_the_channel_and_maps_each_pr_to_its_message(monkeypatch):
    from cyclecontrol import slack

    reply = (
        '```json\n[{"pr": "https://github.com/o/r/pull/1760/changes", "ts": "1793783732.377339"},'
        ' {"pr": "https://github.com/o/r/pull/1765", "ts": "1793783732.377339"},'
        ' {"pr": "https://github.com/o/r/pull/1760", "ts": "1793700000.000001"}]\n```'
    )
    monkeypatch.setattr(
        slack.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, reply, "")
    )
    found = slack.posted_pr_links("claude", "C1", "sporthub")
    assert found == {
        "https://github.com/o/r/pull/1760": "https://sporthub.slack.com/archives/C1/p1793783732377339",
        "https://github.com/o/r/pull/1765": "https://sporthub.slack.com/archives/C1/p1793783732377339",
    }, "the suffix is dropped and the newest message wins"


def test_posted_pr_links_fails_loudly_on_prose(monkeypatch):
    from cyclecontrol import slack

    monkeypatch.setattr(
        slack.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, "I cannot read that channel", ""),
    )
    with pytest.raises(slack.SlackError):
        slack.posted_pr_links("claude", "C1", "sporthub")


def test_linear_retries_once_when_a_request_times_out(monkeypatch):
    import httpx

    from cyclecontrol import linear

    client = linear.LinearClient("key", "SLM")
    calls = []

    def post(url, json):
        calls.append(json["query"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(
            200,
            json={"data": {"viewer": {"id": "me"}}},
            request=httpx.Request("POST", linear.API_URL),
        )

    monkeypatch.setattr(client._http, "post", post)

    assert client._post("query { viewer { id } }", {}) == {"viewer": {"id": "me"}}
    assert len(calls) == 2


def test_pr_links_are_read_from_anywhere_in_the_phase_note(tmp_path):
    """SLM-1428 wrote "Stack, merge in order: <url> ... then <url>" and the board saw no PR at all."""
    import json

    from cyclecontrol.progress import read_progress

    (tmp_path / "spp-phase.json").write_text(
        json.dumps(
            {
                "phase": "human",
                "note": "Stack, merge in order: https://github.com/o/r/pull/1837 (editor), "
                "then https://github.com/o/r/pull/1838 (stacked on #1837)",
            }
        )
    )

    progress = read_progress(tmp_path)

    assert progress.pr_url == "https://github.com/o/r/pull/1837 https://github.com/o/r/pull/1838"


def test_cancelled_issues_are_dropped_from_the_cycle_and_the_family():
    from cyclecontrol.linear import merge_family, parse_dashboard

    def node(ident, state_type, parent=None):
        return {
            "identifier": ident,
            "title": ident,
            "url": "",
            "branchName": "",
            "priority": 0,
            "createdAt": "",
            "team": {"key": "SLM"},
            "parent": {"identifier": parent} if parent else None,
            "children": {"nodes": []},
            "state": {"name": state_type, "type": state_type},
            "assignee": None,
            "inverseRelations": {"nodes": []},
        }

    data = {
        "viewer": {"assignedIssues": {"nodes": []}},
        "cycles": {
            "nodes": [
                {
                    "number": 1,
                    "endsAt": "2099-01-01T00:00:00+00:00",
                    "issues": {"nodes": [node("SLM-1", "started"), node("SLM-2", "canceled")]},
                }
            ]
        },
    }
    cycle, issues = parse_dashboard(data)
    assert [i.id for i in issues] == ["SLM-1"]
    assert cycle.total == 1
    parent = node("SLM-9", "started")
    parent["children"] = {
        "nodes": [node("SLM-3", "canceled", "SLM-9"), node("SLM-4", "unstarted", "SLM-9")]
    }
    merged = merge_family(issues, {"p": parent}, set())
    assert [i.id for i in merged] == ["SLM-1", "SLM-9", "SLM-4"]
