import json
from pathlib import Path

from cyclecontrol import memwatch, sessions
from cyclecontrol.activity import TranscriptReader, project_dir
from cyclecontrol.memwatch import (
    BuilderState,
    ComposeStack,
    MemoryReading,
    MemoryWatchdog,
    parse_compose_ls,
    select_stacks_to_stop,
)
from cyclecontrol.sessions import MEMORY_NUDGE_AFTER_SECONDS, SessionManager
from tests.test_sessions import ChattyPty, add_session, issue, settings_for

REAPED = (
    "<task-notification>\n<task-id>b8</task-id>\n<status>killed</status>\n"
    '<summary>Background command "cd ~/code/street-slm-1718 &amp;&amp; make worktree-provision DB=isolated" '
    "was stopped because the system is running low on memory</summary>\n"
    "<note>This is not a failure of the command. Claude Code stopped it because the system was critically low "
    "on memory while the session was idle.</note>\n</task-notification>"
)


def user(text, at="2026-11-20T16:44:56Z"):
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}


def say(text, at="2026-11-20T16:45:08Z"):
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def bash(ident, command, at="2026-11-20T16:40:00Z"):
    block = {"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "timestamp": at, "message": {"content": [block]}}


def result(ident, content, error=False, at="2026-11-20T16:41:00Z"):
    block = {"type": "tool_result", "tool_use_id": ident, "content": content, "is_error": error}
    return {"type": "user", "timestamp": at, "message": {"content": [block]}}


def write(path: Path, events: list[dict], append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def reader_for(tmp_path, events) -> TranscriptReader:
    path = tmp_path / "t.jsonl"
    write(path, events)
    reader = TranscriptReader(path)
    reader.refresh()
    return reader


def test_a_reaped_background_command_is_a_memory_kill_until_the_next_prompt(tmp_path):
    reader = reader_for(
        tmp_path, [user("Build it"), user(REAPED), say("Stopped; not restarting it.")]
    )
    assert reader.memory_kill["step"].startswith(
        "cd ~/code/street-slm-1718 && make worktree-provision"
    )

    write(reader.path, [user("wanna try again?", at="2026-11-23T06:53:59Z")], append=True)
    reader.refresh()
    assert reader.memory_kill is None, "Elliot's prompt starts a new turn"


def test_an_exit_137_container_job_is_a_kill_and_carrying_on_clears_it(tmp_path):
    events = [
        bash("t1", "docker compose run --rm app php artisan test"),
        result("t1", "Exit code 137\nKilled", True),
    ]
    reader = reader_for(tmp_path, events)
    assert reader.memory_kill["step"] == "docker compose run --rm app php artisan test"

    more = []
    for n in range(5):
        more += [bash(f"x{n}", "git status"), result(f"x{n}", "clean")]
    write(reader.path, more, append=True)
    reader.refresh()
    assert reader.memory_kill is None, "five more tool calls means the builder carried on"


def test_a_node_heap_out_of_memory_is_not_a_memory_kill(tmp_path):
    events = [
        bash("t1", "npm run lint"),
        result("t1", "Exit code 134\nJavaScript heap out of memory", True),
    ]
    assert reader_for(tmp_path, events).memory_kill is None


def memory_session(tmp_path, issue_id="SLM-1718", phase="build"):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue(issue_id, "ABC"), phase=phase)
    session.transcript_home = tmp_path / "home"
    session.pty = ChattyPty("")
    session.pty.last_output = 1000.0
    sent = []
    session.pty.write = lambda data: sent.append(data.decode())
    path = project_dir(session.worktree, session.transcript_home) / "s.jsonl"
    write(path, [user("Build it"), user(REAPED), say("Stopped; not restarting it.")])
    return manager, session, sent, path


def test_a_builder_stopped_by_a_memory_kill_is_woken_once_then_escalated_once(
    tmp_path, monkeypatch
):
    notes = []
    monkeypatch.setattr(sessions, "notify", lambda title, message: notes.append(title))
    manager, session, sent, path = memory_session(tmp_path)

    assert manager.nudge_memory_kill(now=1000.0 + MEMORY_NUDGE_AFTER_SECONDS - 1) == ([], [])
    woken, stuck = manager.nudge_memory_kill(now=1000.0 + MEMORY_NUDGE_AFTER_SECONDS)
    assert [s.issue.id for s in woken] == ["SLM-1718"] and stuck == []
    assert "retry the smallest failed step once, one container job at a time" in sent[0]
    assert "make worktree-provision" in sent[0]
    assert manager.nudge_memory_kill(now=5000.0) == ([], []), "the same kill is handled once"

    write(
        path,
        [user(sent[0], at="2026-11-20T17:00:00Z"), user(REAPED, at="2026-11-20T17:05:00Z")],
        append=True,
    )
    session.pty.last_output = 6000.0
    woken, stuck = manager.nudge_memory_kill(now=7000.0)
    assert woken == [] and [s.issue.id for s in stuck] == ["SLM-1718"]
    assert len(sent) == 1, "no second retry"
    assert notes == ["SLM-1718 needs you: killed for memory again"]

    write(
        path,
        [user("go", at="2026-11-20T18:00:00Z"), user(REAPED, at="2026-11-20T18:05:00Z")],
        append=True,
    )
    assert manager.nudge_memory_kill(now=9000.0) == ([], []), "Elliot is told once"
    assert len(notes) == 1


def test_a_handed_over_builder_is_not_woken_for_a_reaped_leftover(tmp_path):
    manager, _, sent, _ = memory_session(tmp_path, phase="human")
    assert manager.nudge_memory_kill(now=5000.0) == ([], [])
    assert sent == []


def test_a_builder_whose_own_process_was_sigkilled_is_woken_with_its_conversation(
    tmp_path, monkeypatch
):
    manager, session, _, path = memory_session(tmp_path)
    write(path, [user("Build it"), say("Working")])
    session.pty.alive = False
    session.pty.pid = 4242
    session.pty.exit_status = lambda: 1
    session.pty.exit_signal = lambda: 9
    calls = []
    monkeypatch.setattr(
        manager, "wake", lambda issue_id, prompt="": calls.append((issue_id, prompt)) or "started"
    )

    woken, _ = manager.nudge_memory_kill(now=5000.0)

    assert [s.issue.id for s in woken] == ["SLM-1718"]
    assert calls[0][0] == "SLM-1718" and "one container job at a time" in calls[0][1]
    assert session.memory_nudges == 1 and sessions.flags_of(session)["memoryNudges"] == 1


def test_a_builder_that_simply_exited_is_left_alone(tmp_path, monkeypatch):
    manager, session, _, path = memory_session(tmp_path)
    write(path, [user("Build it"), say("Done")])
    session.pty.alive = False
    session.pty.exit_status = lambda: 0
    session.pty.exit_signal = lambda: None
    monkeypatch.setattr(manager, "wake", lambda *a: (_ for _ in ()).throw(AssertionError("woken")))
    assert manager.nudge_memory_kill(now=5000.0) == ([], [])


def test_green_ci_nudges_twice_then_tells_elliot_once(tmp_path, monkeypatch):
    from cyclecontrol import server

    notes = []
    monkeypatch.setattr(server, "notify", lambda title, message: notes.append(title))
    app = server.App(settings_for(tmp_path, comments_file=str(tmp_path / "c.json")))
    session = add_session(app.sessions, tmp_path, issue("SLM-1516", "ABC"), phase="ci")
    session.pty = ChattyPty("waiting on CircleCI\n")
    sent = []
    session.pty.write = lambda data: sent.append(data)
    view = {"hasPr": True, "prNumber": 1900, "status": {"open": True, "ci": "SUCCESS"}}
    monkeypatch.setattr(app.comments, "view", lambda s: view)

    for stall in range(4):
        session.pty.last_output = 10_000.0 * (stall + 1)
        app.nudge_green_ci(now=session.pty.last_output + 700)

    assert session.ci_nudges == 2
    assert len([d for d in sent if d != b"\r"]) == 2
    assert notes == ["SLM-1516 needs you: CI is green but it will not hand over"]


def stack(name, folder, status="running(3)"):
    return ComposeStack(name, status, (f"{folder}/compose.worktree.yml",))


def builder(issue_id, folder, phase="build", asleep=False, finished=False, alive=False):
    return BuilderState(issue_id, Path(folder), phase, asleep, finished, alive)


def test_only_running_stacks_of_idle_builders_are_picked(tmp_path):
    stacks = [
        stack("partner-slm-1", tmp_path / "slm-1"),
        stack("partner-slm-2", tmp_path / "slm-2"),
        stack("partner-slm-3", tmp_path / "slm-3"),
        stack("partner-slm-4", tmp_path / "slm-4"),
        stack("partner-slm-5", tmp_path / "slm-5", status="exited(3)"),
        stack("partner-slm-6", tmp_path / "slm-6"),
        stack("consumer-v3", tmp_path / "consumer-v3"),
    ]
    builders = [
        builder("SLM-1", tmp_path / "slm-1", phase="human"),
        builder("SLM-2", tmp_path / "slm-2", asleep=True),
        builder("SLM-3", tmp_path / "slm-3", finished=True),
        builder("SLM-4", tmp_path / "slm-4", phase="human", alive=True),
        builder("SLM-5", tmp_path / "slm-5", finished=True),
        builder("SLM-6", tmp_path / "slm-6", phase="build"),
    ]
    picked = select_stacks_to_stop(stacks, builders)
    assert [(c.stack, c.issue_id) for c in picked] == [
        ("partner-slm-1", "SLM-1"),
        ("partner-slm-2", "SLM-2"),
        ("partner-slm-3", "SLM-3"),
    ]


def test_compose_ls_output_is_parsed():
    text = json.dumps(
        [
            {
                "Name": "partner-slm-1500",
                "Status": "running(1)",
                "ConfigFiles": "/a/compose.yml,/a/x.yml",
            }
        ]
    )
    assert parse_compose_ls(text) == [
        ComposeStack("partner-slm-1500", "running(1)", ("/a/compose.yml", "/a/x.yml"))
    ]
    assert parse_compose_ls("not json") == []


def test_memory_reading_is_low_on_critical_pressure_or_little_free_memory():
    assert MemoryReading(10).low and MemoryReading(40, 4).low
    assert not MemoryReading(47, 2).low and not MemoryReading(None).low


def test_the_watchdog_only_notifies_and_not_twice_for_the_same_stacks(tmp_path):
    manager = SessionManager(settings_for(tmp_path))
    session = add_session(manager, tmp_path, issue("SLM-1", "ABC"), phase="human", alive=False)
    notes = []
    reading = {"value": MemoryReading(8)}
    stacks = [ComposeStack("partner-slm-1", "running(2)", (str(session.worktree / "compose.yml"),))]
    watchdog = MemoryWatchdog(
        manager,
        lambda title, message: notes.append((title, message)),
        probe=lambda: reading["value"],
        list_stacks=lambda: stacks,
    )

    assert [c.stack for c in watchdog.check(now=100.0)] == ["partner-slm-1"]
    assert (
        notes
        and "would stop 1 docker stack" in notes[0][0]
        and "Nothing was stopped" in notes[0][1]
    )
    watchdog.check(now=200.0)
    assert len(notes) == 1, "same stacks, not again so soon"
    watchdog.check(now=100.0 + memwatch.RENOTIFY_SECONDS)
    assert len(notes) == 2

    reading["value"] = MemoryReading(50)
    assert watchdog.check(now=99_999.0) == []
    assert not hasattr(watchdog, "stop"), "notify-only: there is no way to stop a stack"
