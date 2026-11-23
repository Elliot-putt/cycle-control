"""A claude process in a pseudo-terminal, with scrollback for late viewers."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable

import ptyprocess

logger = logging.getLogger(__name__)

MAX_SCROLLBACK_BYTES = 256 * 1024
ANSI = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]"
)


def latest_line(tail: bytes) -> str:
    """The last readable line in a chunk of raw terminal output."""
    cleaned = ANSI.sub(b"", tail).decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", cleaned)]
    readable = [ln for ln in lines if len(ln) > 3 and any(ch.isalnum() for ch in ln)]
    return readable[-1][:160] if readable else ""


class PtySession:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
        self._argv = argv
        self._cwd = cwd
        self._extra_env = env or {}
        self._proc: ptyprocess.PtyProcess | None = None
        self._lock = threading.Lock()
        self._fan_lock = threading.Lock()
        self._readers: list[Callable[[bytes], None]] = []
        self.scrollback: list[bytes] = []
        self._scrollback_bytes = 0
        self.last_output = time.time()

    def attach(self, reader: Callable[[bytes], None]) -> list[bytes]:
        """Register a live reader and return the scrollback so far, with no gap or overlap."""
        with self._fan_lock:
            self._readers.append(reader)
            return list(self.scrollback)

    def detach(self, reader: Callable[[bytes], None]) -> None:
        with self._fan_lock:
            if reader in self._readers:
                self._readers.remove(reader)

    def start(self, rows: int = 40, cols: int = 140) -> None:
        env = {**os.environ, **self._extra_env}
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        env.setdefault("LANG", "en_US.UTF-8")
        self._proc = ptyprocess.PtyProcess.spawn(
            self._argv, cwd=self._cwd, dimensions=(rows, cols), env=env
        )
        threading.Thread(target=self._read_loop, daemon=True, name="pty-reader").start()

    def _read_loop(self) -> None:
        assert self._proc is not None
        while True:
            try:
                data = self._proc.read(4096)
            except EOFError:
                break
            except Exception:
                logger.exception("PTY read failed")
                break
            self.last_output = time.time()
            with self._fan_lock:
                self.scrollback.append(data)
                self._scrollback_bytes += len(data)
                while self._scrollback_bytes > MAX_SCROLLBACK_BYTES and self.scrollback:
                    self._scrollback_bytes -= len(self.scrollback.pop(0))
                for reader in list(self._readers):
                    try:
                        reader(data)
                    except Exception:
                        logger.exception("PTY reader raised")

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._proc and self._proc.isalive():
                self._proc.write(data)

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            if self._proc and self._proc.isalive():
                self._proc.setwinsize(rows, cols)

    @property
    def pid(self) -> int:
        return self._proc.pid if self._proc else 0

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.isalive())

    def terminate(self) -> None:
        with self._lock:
            if self._proc and self._proc.isalive():
                self._proc.terminate(force=True)

    def exit_status(self) -> int | None:
        """None while running, otherwise the exit code (a signal counts as 1)."""
        if self._proc is None or self._proc.isalive():
            return None
        return self._proc.exitstatus if self._proc.exitstatus is not None else 1

    def exit_signal(self) -> int | None:
        """The signal that ended the process, or None while running or after a normal exit.
        macOS's low-memory killer (jetsam) sends SIGKILL, which a shell would report as exit 137."""
        if self._proc is None or self._proc.isalive():
            return None
        return self._proc.signalstatus

    def tail(self, limit: int = 6000) -> bytes:
        parts: list[bytes] = []
        size = 0
        with self._fan_lock:
            for chunk in reversed(self.scrollback):
                parts.append(chunk)
                size += len(chunk)
                if size >= limit:
                    break
        return b"".join(reversed(parts))[-limit:]


class SleepingPty:
    """Stands in for a builder that is not running: same surface as PtySession, no process, no scrollback."""

    def __init__(self) -> None:
        self.scrollback: list[bytes] = []
        self.last_output = time.time()
        self.pid = 0

    def attach(self, reader: Callable[[bytes], None]) -> list[bytes]:
        return []

    def detach(self, reader: Callable[[bytes], None]) -> None:
        pass

    def write(self, data: bytes) -> None:
        pass

    def resize(self, rows: int, cols: int) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def terminate(self) -> None:
        pass

    def exit_status(self) -> int | None:
        return None

    def exit_signal(self) -> int | None:
        return None

    def tail(self, limit: int = 6000) -> bytes:
        return b""
