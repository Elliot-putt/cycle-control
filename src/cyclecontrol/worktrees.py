"""Plain git worktrees for builders. Never a Docker stack."""

from __future__ import annotations

import subprocess
from pathlib import Path

from cyclecontrol.config import TeamConfig
from cyclecontrol.models import Issue


class WorktreeError(RuntimeError):
    pass


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True, timeout=180)


def remote_sha(repo: Path, branch: str) -> str:
    result = _git("-C", str(repo), "ls-remote", "origin", f"refs/heads/{branch}")
    return result.stdout.split()[0] if result.returncode == 0 and result.stdout.strip() else ""


def ensure_worktree(team: TeamConfig, issue: Issue, base: str) -> Path:
    """Fetch the base branch and add the issue's worktree from it, reusing one that exists."""
    path = team.worktree_path(issue.id)
    repo = str(team.repo_path)
    if path.exists():
        return path
    fetched = _git("-C", repo, "fetch", "origin", base)
    if fetched.returncode != 0:
        raise WorktreeError(f"git fetch failed: {fetched.stderr.strip()}")
    added = _git("-C", repo, "worktree", "add", str(path), "-b", issue.branch, f"origin/{base}")
    if added.returncode != 0 and "already exists" in added.stderr:
        added = _git("-C", repo, "worktree", "add", str(path), issue.branch)
    if added.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {added.stderr.strip()}")
    return path


def ensure_worktree_for_branch(team: TeamConfig, issue: Issue, branch: str) -> Path:
    """A worktree on an existing remote branch, such as a PR's head."""
    path = team.worktree_path(issue.id)
    if path.exists():
        return path
    repo = str(team.repo_path)
    fetched = _git("-C", repo, "fetch", "origin", branch)
    if fetched.returncode != 0:
        raise WorktreeError(f"git fetch failed: {fetched.stderr.strip()}")
    added = _git("-C", repo, "worktree", "add", str(path), "-b", branch, f"origin/{branch}")
    if added.returncode != 0 and "already exists" in added.stderr:
        added = _git("-C", repo, "worktree", "add", str(path), branch)
    if added.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {added.stderr.strip()}")
    return path


def needs_provision(path: Path) -> bool:
    """True when the worktree has never been provisioned: no APP_SLUG in .env or no ready marker."""
    try:
        has_slug = any(
            line.startswith("APP_SLUG=") and line.split("=", 1)[1].strip(" '\"")
            for line in (path / ".env").read_text().splitlines()
        )
        ready = (path / ".claude" / ".worktree-ready").read_text().strip()
    except OSError:
        return True
    return not has_slug or ready in ("", "none")


def has_provision_recipe(path: Path) -> bool:
    """True when the repo defines `just worktree-provision` (consumer-v3 does not)."""
    try:
        found = subprocess.run(
            ["just", "--show", "worktree-provision"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return found.returncode == 0


def provision(path: Path) -> bool:
    """Provision the worktree; False (and nothing run) when the repo has no recipe for it."""
    if not has_provision_recipe(path):
        return False
    result = subprocess.run(
        ["just", "worktree-provision"],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise WorktreeError(f"worktree-provision failed: {result.stderr.strip()[-300:]}")
    return True


def git_dir(path: Path) -> Path:
    result = _git("-C", str(path), "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise WorktreeError(f"not a git worktree: {path}")
    return Path(result.stdout.strip())
