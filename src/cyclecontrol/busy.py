from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable

WORK = re.compile(
    r"gates\.sh|\bjust (lint|test|test-\w+|types|build|format)\b|vendor/bin/(pest|phpunit|phpstan|pint)|\bvitest\b|artisan test|\bpest\b|worktree-provision"
)
CACHE_SECONDS = 5.0


def read_process_table() -> list[tuple[int, int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


class WorkWatcher:
    """True while a builder has a gates, lint or test run going under it, so an idle terminal is not read as waiting on a person."""

    def __init__(
        self,
        table: Callable[[], list[tuple[int, int, str]]] = read_process_table,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._table = table
        self._clock = clock
        self._read_at: float | None = None
        self._children: dict[int, list[tuple[int, str]]] = {}

    def running(self, root: int) -> bool:
        if root <= 0:
            return False
        now = self._clock()
        if self._read_at is None or now - self._read_at >= CACHE_SECONDS:
            self._children = self._snapshot()
            self._read_at = now
        return self._look(root)

    def _snapshot(self) -> dict[int, list[tuple[int, str]]]:
        """One process table shared by every builder, so the cost does not grow with the builder count."""
        try:
            rows = self._table()
        except (OSError, subprocess.SubprocessError):
            return {}
        children: dict[int, list[tuple[int, str]]] = {}
        for pid, parent, command in rows:
            children.setdefault(parent, []).append((pid, command))
        return children

    def _look(self, root: int) -> bool:
        children = self._children
        stack = [root]
        seen = {root}
        while stack:
            for pid, command in children.get(stack.pop(), []):
                if pid in seen:
                    continue
                seen.add(pid)
                if WORK.search(command):
                    return True
                stack.append(pid)
        return False


WATCHER = WorkWatcher()
