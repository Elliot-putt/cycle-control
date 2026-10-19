"""One builder per worktree: a lock file in the git dir, and a check that an old process is really gone."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

LOCK_NAME = "spp-builder.lock"
PENDING_SECONDS = 120


class BuilderLockedError(RuntimeError):
    pass


class Procs:
    """Looks at real processes. Tests swap this for a fake."""

    def claude_running(self, pid: int) -> bool:
        """True when the pid is a live claude process. A zombie or a reused pid is not."""
        if pid <= 0:
            return False
        result = subprocess.run(
            ["ps", "-o", "stat=,command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        line = result.stdout.strip()
        if not line:
            return False
        stat, _, command = line.partition(" ")
        return not stat.startswith("Z") and "claude" in command

    def claude_pids_in(self, worktree: Path) -> list[int]:
        """Claude processes whose working directory is the worktree, whoever started them."""
        result = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-c", "claude", "-Fpn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        target = str(worktree.resolve())
        found: list[int] = []
        pid = 0
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid:
                try:
                    if str(Path(line[1:]).resolve()) == target:
                        found.append(pid)
                except OSError:
                    continue
        return found


PROCS = Procs()


def _read(git_dir: Path) -> dict | None:
    try:
        data = json.loads((git_dir / LOCK_NAME).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def holder(git_dir: Path, procs: Procs = PROCS) -> dict | None:
    """The live holder of the lock, or None when there is no lock or it is stale."""
    lock = _read(git_dir)
    if lock is None:
        return None
    pid = int(lock.get("pid", 0))
    if pid > 0:
        return lock if procs.claude_running(pid) else None
    fresh = time.time() - float(lock.get("at", 0)) < PENDING_SECONDS
    return lock if fresh and _app_alive(int(lock.get("app", 0))) else None


def _app_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire(git_dir: Path, worktree: Path, issue_id: str, procs: Procs = PROCS) -> None:
    """Take the lock before starting a builder. Refuses while another builder is alive in the worktree."""
    held = holder(git_dir, procs)
    if held:
        raise BuilderLockedError(
            f"A builder is already running in {worktree} (pid {held.get('pid') or 'starting'})."
        )
    strays = procs.claude_pids_in(worktree)
    if strays:
        raise BuilderLockedError(
            f"claude is already running in {worktree} (pid {strays[0]}), outside this lock."
        )
    path = git_dir / LOCK_NAME
    path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as failure:
        raise BuilderLockedError(f"Another builder is starting in {worktree}.") from failure
    with os.fdopen(fd, "w") as handle:
        json.dump(
            {
                "pid": 0,
                "issue": issue_id,
                "worktree": str(worktree),
                "app": os.getpid(),
                "at": time.time(),
            },
            handle,
        )


def set_pid(git_dir: Path, pid: int) -> None:
    lock = _read(git_dir) or {}
    lock["pid"] = pid
    (git_dir / LOCK_NAME).write_text(json.dumps(lock))


def release(git_dir: Path, pid: int | None = None) -> None:
    """Drop the lock, but only your own when a pid is given."""
    lock = _read(git_dir)
    if lock is None:
        return
    if pid is None or int(lock.get("pid", 0)) in (pid, 0):
        (git_dir / LOCK_NAME).unlink(missing_ok=True)


def confirm_gone(git_dir: Path, worktree: Path, procs: Procs = PROCS) -> tuple[bool, str]:
    """True only when no live builder holds the lock and no claude process sits in the worktree."""
    held = holder(git_dir, procs)
    if held:
        return False, f"pid {held.get('pid') or 'starting'} still holds the lock"
    strays = procs.claude_pids_in(worktree)
    if strays:
        return False, f"claude (pid {strays[0]}) is still running in the worktree"
    return True, ""
