"""Turns a builder's Claude transcript into the activity feed: messages, collapsed tool calls, questions."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_ENTRIES = 300
OPTION = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
EXIT_CODE = re.compile(r"Exit code (\d+)")
PASSED = re.compile(r"\b(\d+) passed\b")


def project_dir(worktree: Path, home: Path | None = None) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(worktree))
    return (home or Path.home()) / ".claude" / "projects" / slug


def newest_transcript(worktree: Path, home: Path | None = None) -> Path | None:
    files = list(project_dir(worktree, home).glob("*.jsonl"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _clock(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


def _seconds(start: str, end: str) -> int:
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
    except ValueError:
        return 0
    return max(0, int((b - a).total_seconds()))


def span(seconds: int) -> str:
    return f"{seconds}s" if seconds < 90 else f"{round(seconds / 60)}m"


def target_of(name: str, args: dict[str, Any]) -> str:
    if name == "Bash":
        return (
            str(args.get("command", "")).strip().splitlines()[0][:90] if args.get("command") else ""
        )
    for key in ("file_path", "path", "pattern", "description", "skill", "query"):
        if args.get(key):
            return str(args[key])[:90]
    for value in args.values():
        if isinstance(value, str):
            return value[:90]
    return ""


def options_in(text: str) -> list[dict[str, str]]:
    found = [OPTION.match(line) for line in text.splitlines()]
    return [{"key": m.group(1), "label": m.group(2)} for m in found if m]


def _result_word(content: Any, is_error: bool) -> tuple[str, bool]:
    text = content if isinstance(content, str) else json.dumps(content)
    if is_error:
        code = EXIT_CODE.search(text)
        return (f"exit {code.group(1)}" if code else "failed"), False
    passed = PASSED.search(text)
    return (f"{passed.group(1)} passed" if passed else "ok"), True


class TranscriptReader:
    """Reads a transcript incrementally and keeps the feed in memory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._carry = b""
        self.entries: list[dict[str, Any]] = []
        self._calls: dict[str, dict[str, Any]] = {}
        self._asking: dict[str, dict[str, Any]] = {}
        self.last_event_at = ""
        self.last_say_open = False

    def refresh(self) -> None:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return
        data = self._carry + chunk
        lines = data.split(b"\n")
        self._carry = lines.pop()
        self._offset += len(chunk)
        for raw in lines:
            try:
                self._take(json.loads(raw))
            except ValueError:
                continue
        del self.entries[:-MAX_ENTRIES]

    def _group(self) -> dict[str, Any]:
        if self.entries and self.entries[-1]["kind"] == "tools":
            return self.entries[-1]
        group = {"kind": "tools", "count": 0, "failed": 0, "start": "", "end": "", "calls": []}
        self.entries.append(group)
        return group

    def _take(self, event: dict[str, Any]) -> None:
        if event.get("isSidechain") or event.get("type") not in ("assistant", "user"):
            return
        stamp = str(event.get("timestamp", ""))
        content = (event.get("message") or {}).get("content")
        if event["type"] == "user":
            self._user(content, stamp)
            return
        self.last_event_at = stamp or self.last_event_at
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                text = str(block["text"]).strip()
                self.entries.append({"kind": "say", "at": _clock(stamp), "text": text})
                self.last_say_open = True
            elif block.get("type") == "tool_use":
                self._tool_use(block, stamp)

    def _tool_use(self, block: dict[str, Any], stamp: str) -> None:
        name = str(block.get("name", ""))
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        if name in ("AskUserQuestion", "ExitPlanMode"):
            self._asking[str(block.get("id"))] = {"name": name, "input": args}
            return
        group = self._group()
        call = {"name": name, "target": target_of(name, args), "result": "", "ok": True}
        group["calls"].append(call)
        group["count"] += 1
        group["start"] = group["start"] or stamp
        group["end"] = stamp
        self._calls[str(block.get("id"))] = {"call": call, "group": group}

    def _user(self, content: Any, stamp: str) -> None:
        if isinstance(content, str) or not isinstance(content, list):
            self._asking.clear()
            self.last_say_open = False
            return
        for block in content:
            if block.get("type") == "tool_result":
                self._tool_result(block, stamp)
            elif block.get("type") == "text":
                self._asking.clear()
                self.last_say_open = False

    def _tool_result(self, block: dict[str, Any], stamp: str) -> None:
        use_id = str(block.get("tool_use_id"))
        self._asking.pop(use_id, None)
        found = self._calls.pop(use_id, None)
        if not found:
            return
        word, ok = _result_word(block.get("content"), bool(block.get("is_error")))
        found["call"]["result"] = word
        found["call"]["ok"] = ok
        found["group"]["end"] = stamp or found["group"]["end"]
        if not ok:
            found["group"]["failed"] += 1

    def question(self, is_waiting: bool) -> dict[str, Any] | None:
        for asked in self._asking.values():
            args = asked["input"]
            if asked["name"] == "ExitPlanMode":
                return {"kind": "plan", "text": str(args.get("plan", "")), "options": []}
            first = (args.get("questions") or [{}])[0]
            options = [
                {"key": str(i), "label": str(o.get("label", ""))}
                for i, o in enumerate(first.get("options", []), start=1)
            ]
            return {"kind": "menu", "text": str(first.get("question", "")), "options": options}
        if is_waiting and self.last_say_open:
            last = next((e for e in reversed(self.entries) if e["kind"] == "say"), None)
            if last:
                return {"kind": "text", "text": last["text"], "options": options_in(last["text"])}
        return None

    def latest_tool(self) -> str:
        for entry in reversed(self.entries):
            if entry["kind"] == "tools" and entry["calls"]:
                call = entry["calls"][-1]
                return f"{call['name']} · {call['target']}"[:80]
        return ""

    def feed(self) -> list[dict[str, Any]]:
        shown: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry["kind"] == "say":
                shown.append(entry)
                continue
            seconds = _seconds(entry["start"], entry["end"])
            tail = f"{entry['failed']} failed" if entry["failed"] else span(seconds)
            shown.append(
                {
                    "kind": "tools",
                    "summary": f"{entry['count']} tool call{'s' if entry['count'] != 1 else ''} · {tail}",
                    "failed": entry["failed"],
                    "calls": entry["calls"],
                }
            )
        return shown


def write_demo_transcript(directory: Path) -> None:
    """A short fake transcript so the demo shows messages, tool calls, a failure and a question."""
    directory.mkdir(parents=True, exist_ok=True)

    def say(minute: int, text: str) -> dict[str, Any]:
        return {
            "type": "assistant",
            "timestamp": f"2026-10-19T10:{minute:02d}:00Z",
            "message": {"content": [{"type": "text", "text": text}]},
        }

    def tool(
        minute: int, second: int, ident: str, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": "assistant",
            "timestamp": f"2026-10-19T10:{minute:02d}:{second:02d}Z",
            "message": {
                "content": [{"type": "tool_use", "id": ident, "name": name, "input": args}]
            },
        }

    def result(
        minute: int, second: int, ident: str, content: str, error: bool = False
    ) -> dict[str, Any]:
        return {
            "type": "user",
            "timestamp": f"2026-10-19T10:{minute:02d}:{second:02d}Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": ident,
                        "content": content,
                        "is_error": error,
                    }
                ]
            },
        }

    events = [
        say(
            41,
            "PR 1 is ready for you to review:\n\nhttps://github.com/AgentSoftware/street-partner-platform/pull/1479\n\n"
            "- **State:** it's open and not a draft. The branch is behind `develop` with no conflicts.\n"
            "- **CI fix:** `lint-js-types` ran out of memory because of the `date-fns` root import.\n"
            "- **Local checks:** parity matched across six time zones, and 1,805 tests pass.\n\n"
            "| PR | Issue | Depends on |\n|---|---|---|\n| 1 | PP-1039 | none |\n| 2 | PP-1040 | PR 1 |",
        ),
        tool(41, 10, "t1", "Bash", {"command": "just npm run testRun -- date.test.ts"}),
        result(41, 40, "t1", "47 passed"),
        tool(42, 0, "t2", "Edit", {"file_path": "resources/js/shared/utils/date.ts"}),
        result(42, 5, "t2", "updated"),
        tool(42, 20, "t3", "Bash", {"command": "python3 patch.py"}),
        result(42, 30, "t3", "Exit code 1\nboom", True),
        say(
            44,
            "Gates found one thing I want your call on. date.ts is 284 lines after the port.\n"
            "1. Split relative-time wording into relativeTime.ts\n"
            "2. Keep one file and justify it in the PR",
        ),
    ]
    (directory / "demo.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
