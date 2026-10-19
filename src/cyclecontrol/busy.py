from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable

WORK = re.compile(
    r"gates\.sh|\bjust (lint|test|test-\w+|types|build|format)\b|vendor/bin/(pest|phpunit|phpstan|pint)|\bvitest\b|artisan test|\bpest\b"
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
        self._cached: dict[int, tuple[float, bool]] = {}

    def running(self, root: int) -> bool:
        if root <= 0:
            return False
        hit = self._cached.get(root)
        now = self._clock()
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        answer = self._look(root)
        self._cached[root] = (now, answer)
        return answer

    def _look(self, root: int) -> bool:
        try:
            rows = self._table()
        except (OSError, subprocess.SubprocessError):
            return False
        children: dict[int, list[tuple[int, str]]] = {}
        for pid, parent, command in rows:
            children.setdefault(parent, []).append((pid, command))
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
