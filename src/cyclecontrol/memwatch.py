"""Memory watchdog, notify-only: when the Mac runs low on memory, say which docker compose stacks could go.

It never stops anything. It works out which running stacks belong to builders that are not using them (handed
over at `human`, asleep or finished, with no builder process running) and logs and notifies that it WOULD stop
them, so Elliot can judge whether an automatic stop would have been right before one is ever switched on.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

LOW_FREE_PERCENT = 15
CRITICAL_PRESSURE_LEVEL = 4
RENOTIFY_SECONDS = 1800
CHECK_SECONDS = 60
IDLE_PHASES = ("human",)
FREE_PERCENT = re.compile(r"free percentage:\s*(\d+)%")


@dataclass(frozen=True)
class MemoryReading:
    free_percent: int | None
    pressure_level: int | None = None

    @property
    def low(self) -> bool:
        if self.pressure_level is not None and self.pressure_level >= CRITICAL_PRESSURE_LEVEL:
            return True
        return self.free_percent is not None and self.free_percent < LOW_FREE_PERCENT


@dataclass(frozen=True)
class ComposeStack:
    name: str
    status: str
    config_files: tuple[str, ...]

    @property
    def running(self) -> bool:
        return "running" in self.status


@dataclass(frozen=True)
class BuilderState:
    issue_id: str
    worktree: Path
    phase: str
    asleep: bool
    finished: bool
    alive: bool


@dataclass(frozen=True)
class StopCandidate:
    stack: str
    issue_id: str
    reason: str


def _run(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def macos_memory() -> MemoryReading:
    """`memory_pressure -Q` for the free percentage and the kernel's pressure level (1 normal, 2 warn, 4 critical)."""
    found = FREE_PERCENT.search(_run(["memory_pressure", "-Q"]))
    level = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    return MemoryReading(
        int(found.group(1)) if found else None, int(level) if level.isdigit() else None
    )


def parse_compose_ls(text: str) -> list[ComposeStack]:
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return []
    stacks = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("Name"):
            continue
        files = tuple(f for f in str(row.get("ConfigFiles", "")).split(",") if f)
        stacks.append(ComposeStack(str(row["Name"]), str(row.get("Status", "")), files))
    return stacks


def docker_compose_stacks() -> list[ComposeStack]:
    return parse_compose_ls(_run(["docker", "compose", "ls", "--format", "json"]))


def _inside(path: str, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return False
    return True


def idle_reason(builder: BuilderState) -> str:
    """Why a builder's stack is not in use, or "" when it may be."""
    if builder.alive:
        return ""
    if builder.finished:
        return "finished"
    if builder.phase in IDLE_PHASES:
        return f"at {builder.phase}, builder not running"
    if builder.asleep:
        return "asleep"
    return ""


def select_stacks_to_stop(
    stacks: Iterable[ComposeStack], builders: Iterable[BuilderState]
) -> list[StopCandidate]:
    """Running stacks whose compose files live in the worktree of a builder at human, asleep or finished, with no
    builder process running. A stack no builder owns (the main checkout, another project) is never picked."""
    builders = list(builders)
    picked: list[StopCandidate] = []
    for stack in stacks:
        if not stack.running:
            continue
        owner = next(
            (b for b in builders if any(_inside(f, b.worktree) for f in stack.config_files)),
            None,
        )
        if owner is None:
            continue
        reason = idle_reason(owner)
        if reason:
            picked.append(StopCandidate(stack.name, owner.issue_id, reason))
    return picked


def builder_states(sessions: Iterable) -> list[BuilderState]:
    return [
        BuilderState(
            s.issue.id,
            Path(s.worktree),
            s.progress().phase,
            bool(s.asleep),
            bool(s.finished),
            s.pty.is_alive(),
        )
        for s in sessions
    ]


class MemoryWatchdog:
    """Checks memory and reports which stacks it would stop. Notify-only: it has no way to stop anything."""

    def __init__(
        self,
        sessions,
        notify: Callable[[str, str], None],
        probe: Callable[[], MemoryReading] = macos_memory,
        list_stacks: Callable[[], list[ComposeStack]] = docker_compose_stacks,
    ) -> None:
        self._sessions = sessions
        self._notify = notify
        self._probe = probe
        self._list_stacks = list_stacks
        self._notified_at = 0.0
        self._notified_set: frozenset[str] = frozenset()

    def check(self, now: float | None = None) -> list[StopCandidate]:
        now = time.time() if now is None else now
        reading = self._probe()
        if not reading.low:
            self._notified_set = frozenset()
            return []
        candidates = select_stacks_to_stop(
            self._list_stacks(), builder_states(self._sessions.all())
        )
        names = frozenset(c.stack for c in candidates)
        logger.warning(
            "Memory low (free %s%%, pressure level %s). Would stop: %s",
            reading.free_percent,
            reading.pressure_level,
            ", ".join(f"{c.stack} ({c.issue_id}, {c.reason})" for c in candidates) or "nothing",
        )
        if not candidates:
            return []
        if names == self._notified_set and now - self._notified_at < RENOTIFY_SECONDS:
            return candidates
        self._notified_at, self._notified_set = now, names
        self._notify(
            f"Memory low: would stop {len(candidates)} docker stack{'s' if len(candidates) != 1 else ''}",
            "; ".join(f"{c.stack} ({c.issue_id}, {c.reason})" for c in candidates)
            + ". Nothing was stopped.",
        )
        return candidates
