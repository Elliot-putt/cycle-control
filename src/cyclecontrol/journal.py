"""What the app did on your behalf, kept on disk so it can be read after you were away."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KEEP = 400
ISSUE = re.compile(r"^([A-Z][A-Z0-9]*-\d+)\b")


class Journal:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            lines = self._path.read_text().splitlines()
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines[-KEEP:]:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def record(self, title: str, message: str = "", issue_id: str = "") -> dict[str, Any]:
        found = ISSUE.match(title)
        entry = {
            "at": self._clock(),
            "issue": issue_id or (found.group(1) if found else ""),
            "title": title,
            "message": message,
        }
        with self._lock:
            self._entries.append(entry)
            del self._entries[:-KEEP]
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a") as handle:
                    handle.write(json.dumps(entry) + "\n")
            except OSError:
                logger.warning("Could not write the journal at %s", self._path)
        return entry

    def recent(self, limit: int = 80) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            return list(reversed(self._entries[-limit:]))


_INSTALLED: Journal | None = None


def install(journal: Journal | None) -> None:
    global _INSTALLED
    _INSTALLED = journal


def note(title: str, message: str = "") -> None:
    """A journal line without a desktop notification. Nothing happens when no journal is installed, as in tests."""
    if _INSTALLED is not None:
        _INSTALLED.record(title, message)
