import json
from pathlib import Path

from cyclecontrol.activity import (
    TranscriptReader,
    newest_transcript,
    options_in,
    project_dir,
    span,
    target_of,
)


def write(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def say(text: str, stamp: str = "2026-10-19T10:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def use(ident: str, name: str, args: dict, stamp: str = "2026-10-19T10:00:10Z") -> dict:
    block = {"type": "tool_use", "id": ident, "name": name, "input": args}
    return {"type": "assistant", "timestamp": stamp, "message": {"content": [block]}}


def done(
    ident: str, content: str, error: bool = False, stamp: str = "2026-10-19T10:01:10Z"
) -> dict:
    block = {"type": "tool_result", "tool_use_id": ident, "content": content, "is_error": error}
    return {"type": "user", "timestamp": stamp, "message": {"content": [block]}}


def test_project_dir_slug_matches_claudes_folder_names():
    assert project_dir(Path("/Users/E/code/spp-1035"), Path("/h")) == Path(
        "/h/.claude/projects/-Users-E-code-spp-1035"
    )


def test_newest_transcript_picks_latest_file(tmp_path):
    folder = project_dir(Path("/w/spp-1"), tmp_path)
    folder.mkdir(parents=True)
    (folder / "a.jsonl").write_text("{}\n")
    newer = folder / "b.jsonl"
    newer.write_text("{}\n")
    (folder / "a.jsonl").touch()
    newer.touch()
    assert newest_transcript(Path("/w/spp-1"), tmp_path) == newer
    assert newest_transcript(Path("/w/none"), tmp_path) is None


def test_feed_collapses_tool_calls_between_messages_and_flags_failures(tmp_path):
    path = tmp_path / "t.jsonl"
    write(
        path,
        [
            say("Porting date.ts"),
            use("1", "Bash", {"command": "just test\nsecond line"}),
            done("1", "47 passed"),
            use("2", "Bash", {"command": "python3 patch.py"}),
            done("2", "Exit code 1\nboom", True),
            say("Done with that"),
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "ignore me"}]},
            },
        ],
    )
    reader = TranscriptReader(path)
    reader.refresh()
    feed = reader.feed()
    assert [e["kind"] for e in feed] == ["say", "tools", "say"]
    tools = feed[1]
    assert tools["summary"] == "2 tool calls · 1 failed" and tools["failed"] == 1
    assert [(c["target"], c["result"], c["ok"]) for c in tools["calls"]] == [
        ("just test", "47 passed", True),
        ("python3 patch.py", "exit 1", False),
    ]


def test_reader_is_incremental_and_ignores_partial_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(say("one")) + "\n" + json.dumps(say("two"))[:20])
    reader = TranscriptReader(path)
    reader.refresh()
    assert len(reader.feed()) == 1
    with path.open("a") as handle:
        handle.write(json.dumps(say("two"))[20:] + "\n")
    reader.refresh()
    assert [e["text"] for e in reader.feed()] == ["one", "two"]


def test_ask_user_question_is_pending_until_its_result_arrives(tmp_path):
    path = tmp_path / "t.jsonl"
    ask = {
        "questions": [
            {"question": "Split the file?", "options": [{"label": "Split"}, {"label": "Keep"}]}
        ]
    }
    write(path, [say("Need a call"), use("q1", "AskUserQuestion", ask)])
    reader = TranscriptReader(path)
    reader.refresh()
    question = reader.question(is_waiting=False)
    assert question == {
        "kind": "menu",
        "text": "Split the file?",
        "options": [{"key": "1", "label": "Split"}, {"key": "2", "label": "Keep"}],
    }
    write(path, [say("Need a call"), use("q1", "AskUserQuestion", ask), done("q1", "Split")])
    fresh = TranscriptReader(path)
    fresh.refresh()
    assert fresh.question(is_waiting=False) is None


def test_waiting_with_a_prose_question_offers_its_numbered_options(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, [say("Which way?\n1. Split it\n2) Keep one file")])
    reader = TranscriptReader(path)
    reader.refresh()
    assert reader.question(is_waiting=False) is None
    question = reader.question(is_waiting=True)
    assert question["kind"] == "text"
    assert question["options"] == [
        {"key": "1", "label": "Split it"},
        {"key": "2", "label": "Keep one file"},
    ]


def test_a_user_reply_clears_the_prose_question(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, [say("Which way?"), {"type": "user", "message": {"content": "1"}}])
    reader = TranscriptReader(path)
    reader.refresh()
    assert reader.question(is_waiting=True) is None


def test_helpers():
    assert options_in("a\n1. x\n  2) y\nb") == [
        {"key": "1", "label": "x"},
        {"key": "2", "label": "y"},
    ]
    assert target_of("Edit", {"file_path": "a/b.ts"}) == "a/b.ts"
    assert span(40) == "40s" and span(120) == "2m"
