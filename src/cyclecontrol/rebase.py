"""When a stacked builder's base moves: check the rebase in a scratch worktree, then say what to do.

The builder's own worktree is never touched. A clean rebase becomes an instruction for the builder.
A conflict is never handed to an agent: the lane is marked as needing you.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RebaseCheck:
    clean: bool
    conflicts: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class RebaseAction:
    kind: Literal["none", "rebase", "conflict"]
    new_base: str = ""
    new_sha: str = ""
    text: str = ""
    files: tuple[str, ...] = ()


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True, timeout=180)


def check_rebase(repo: Path, child_ref: str, old_base: str, new_base: str) -> RebaseCheck:
    """Replay old_base..child onto new_base in a throwaway worktree. Reports conflicts, changes nothing else."""
    scratch = Path(tempfile.mkdtemp(prefix="rebase-check-"))
    try:
        added = _git("-C", str(repo), "worktree", "add", "--detach", str(scratch), child_ref)
        if added.returncode != 0:
            return RebaseCheck(False, error=added.stderr.strip())
        rebased = _git("-C", str(scratch), "rebase", "--onto", new_base, old_base)
        if rebased.returncode == 0:
            return RebaseCheck(True)
        files = _git("-C", str(scratch), "diff", "--name-only", "--diff-filter=U").stdout.split()
        _git("-C", str(scratch), "rebase", "--abort")
        return RebaseCheck(False, tuple(files), error="" if files else rebased.stderr.strip()[:200])
    finally:
        _git("-C", str(repo), "worktree", "remove", "--force", str(scratch))
        shutil.rmtree(scratch, ignore_errors=True)
        _git("-C", str(repo), "worktree", "prune")


def decide(
    base: str,
    base_sha: str,
    default_base: str,
    base_tip: str,
    base_merged: bool,
    default_tip: str,
    check: Callable[[str, str], RebaseCheck],
) -> RebaseAction:
    """What to do for one stacked builder, given where its base branch stands."""
    if base == default_base:
        return RebaseAction("none")
    if base_merged and default_tip:
        target, target_sha = default_base, default_tip
        said = f"The PR for your base branch {base} was merged."
        follow = f" Then run `gh pr edit --base {default_base}`."
    elif base_tip and base_sha and base_tip != base_sha:
        target, target_sha = base, base_tip
        said = f"Your base branch {base} has new commits."
        follow = ""
    else:
        return RebaseAction("none")
    result = check(base_sha, target_sha)
    if not result.clean:
        return RebaseAction(
            "conflict", target, target_sha, files=result.conflicts, text=result.error
        )
    text = (
        f"{said} The rebase is clean. Run `git fetch origin && git rebase --onto origin/{target} {base_sha}`."
        f"{follow} Then run the fast gates and `git push --force-with-lease`. "
        "If a conflict appears anyway, stop and tell Elliot; do not resolve it."
    )
    return RebaseAction("rebase", target, target_sha, text)
