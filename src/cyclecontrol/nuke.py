"""Tear one issue down completely: builder, PR, Linear assignment, worktree and containers, board row.

Every step runs even when an earlier one failed, and each reports its own outcome.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cyclecontrol.board import Board
from cyclecontrol.comments import CommentStore
from cyclecontrol.config import Settings, TeamConfig
from cyclecontrol.github import GithubError, PrState, close_pr, find_pr_any_state
from cyclecontrol.models import Issue
from cyclecontrol.sessions import SessionManager

logger = logging.getLogger(__name__)


class NukeRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class StepResult:
    step: str
    ok: bool
    detail: str = ""
    skipped: bool = False


STEPS = ("builder", "pr", "linear", "worktree", "board")


@dataclass
class NukeJob:
    issue: Issue
    started_at: float
    unassign: bool = True
    steps: list[StepResult] = field(default_factory=list)
    current: str = ""
    done: bool = False

    @property
    def failed(self) -> list[StepResult]:
        return [r for r in self.steps if not r.ok]

    def view(self) -> dict[str, Any]:
        return {
            "id": self.issue.id,
            "title": self.issue.title,
            "steps": [asdict(r) for r in self.steps],
            "current": self.current,
            "done": self.done,
            "ok": self.done and not self.failed,
            "failed": [{"step": r.step, "detail": r.detail} for r in self.failed],
        }


def verified_worktree(team: TeamConfig, issue: Issue) -> Path:
    """The issue's worktree, only if it is a real linked worktree under the team's prefix. Anything else is refused."""
    path = team.worktree_path(issue.id)
    repo = team.repo_path.resolve()
    number = issue.id.rsplit("-", 1)[-1]
    resolved = path.resolve()
    if resolved == repo or repo in resolved.parents or resolved in repo.parents:
        raise NukeRefused(f"{path} is the main checkout, refusing.")
    expected = f"{team.worktree_prefix}-{number}"
    if path.name != expected or resolved.name != expected or resolved.parent != repo.parent:
        raise NukeRefused(f"{path} is not the {expected} worktree beside the repo.")
    marker = path / ".git"
    if not marker.is_file():
        raise NukeRefused(f"{path} is not a linked git worktree.")
    if not marker.read_text().startswith("gitdir:"):
        raise NukeRefused(f"{path} has an unexpected .git file.")
    return path


def _lines(result: subprocess.CompletedProcess[str]) -> int:
    return len(result.stdout.splitlines()) if result.returncode == 0 else 0


class Nuker:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionManager,
        board: Board,
        store: CommentStore,
        run: Callable[..., Any] = subprocess.run,
        find_pr: Callable[[str, str], PrState | None] = find_pr_any_state,
        close: Callable[[str, int], None] = close_pr,
        unassign: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._board = board
        self._store = store
        self._run = run
        self._find_pr = find_pr
        self._close = close
        self._unassign = unassign or self._linear_unassign
        self._jobs: dict[str, NukeJob] = {}
        self._jobs_lock = threading.Lock()

    def start(self, issue: Issue, unassign: bool = True) -> str:
        """Begin a nuke: the builder is ended at once, the slow steps run on their own thread.

        Returns started, or running when one is already under way for the issue.
        """
        with self._jobs_lock:
            job = self._jobs.get(issue.id)
            if job and not job.done:
                return "running"
            job = NukeJob(issue, time.time(), unassign)
            self._jobs[issue.id] = job
        job.current = "builder"
        job.steps.append(self._step("builder", self._end_builder, issue))
        threading.Thread(
            target=self._finish_job, args=(job,), daemon=True, name=f"nuke-{issue.id}"
        ).start()
        return "started"

    def _finish_job(self, job: NukeJob) -> None:
        for name, action in (
            ("pr", self._close_pr),
            ("linear", self._unassign_issue if job.unassign else self._keep_assignment),
            ("worktree", self._remove_worktree),
            ("board", self._forget),
        ):
            job.current = name
            job.steps.append(self._step(name, action, job.issue))
        job.current = ""
        job.done = True
        logger.info("Nuked %s: %s", job.issue.id, [asdict(r) for r in job.steps])

    def job(self, issue_id: str) -> NukeJob | None:
        return self._jobs.get(issue_id)

    def dismiss(self, issue_id: str) -> None:
        with self._jobs_lock:
            job = self._jobs.get(issue_id)
            if job and job.done:
                del self._jobs[issue_id]

    def view(self) -> dict[str, dict[str, Any]]:
        return {issue_id: job.view() for issue_id, job in list(self._jobs.items())}

    def _linear_unassign(self, issue_id: str) -> None:
        from cyclecontrol.linear import LinearClient

        LinearClient(self._settings.linear_api_key, self._settings.linear_team_key).unassign(
            issue_id
        )

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            ["git", *args], cwd=str(cwd), check=False, capture_output=True, text=True, timeout=120
        )

    def find_issue(self, issue_id: str) -> Issue | None:
        session = self._sessions.get(issue_id)
        if session:
            return session.issue
        job = self._jobs.get(issue_id)
        if job:
            return job.issue
        return self._board.find(issue_id)

    def preview(self, issue: Issue) -> dict[str, Any]:
        """What Nuke would do for this issue and what would be lost, for the confirm dialog."""
        team = self._settings.team_for(issue.team)
        session = self._sessions.get(issue.id)
        out: dict[str, Any] = {
            "id": issue.id,
            "title": issue.title,
            "branch": issue.branch,
            "builder": session.outcome(self._settings.waiting_after_seconds) if session else "",
            "queued": issue.id in self._sessions.queued_ids(),
            "pr": None,
            "worktree": "",
            "worktreeRefused": "",
            "uncommitted": 0,
            "unpushed": 0,
            "assignee": issue.assignee,
        }
        if team is None:
            out["worktreeRefused"] = f"No repo mapped for team {issue.team}."
            return out
        if self._settings.demo:
            out["worktreeRefused"] = "demo: GitHub and the worktree are not touched"
            return out
        try:
            pr = self._find_pr(team.github_repo, issue.branch)
        except GithubError as failure:
            pr = None
            out["prError"] = str(failure)
        if pr:
            out["pr"] = {"number": pr.number, "url": pr.url, "state": pr.state}
        try:
            path = verified_worktree(team, issue)
        except NukeRefused as refused:
            if team.worktree_path(issue.id).exists():
                out["worktreeRefused"] = str(refused)
            return out
        out["worktree"] = str(path)
        out["uncommitted"] = _lines(self._git(path, "status", "--porcelain"))
        ahead = self._git(path, "log", "--oneline", "@{u}..")
        if ahead.returncode != 0:
            ahead = self._git(path, "log", "--oneline", f"origin/{team.base_branch}..HEAD")
        out["unpushed"] = _lines(ahead)
        return out

    def nuke(self, issue: Issue) -> list[StepResult]:
        """Every step, in order, on the calling thread."""
        job = NukeJob(issue, time.time())
        job.steps.append(self._step("builder", self._end_builder, issue))
        self._finish_job(job)
        return job.steps

    @staticmethod
    def _step(name: str, action: Callable[[Issue], StepResult | str], issue: Issue) -> StepResult:
        try:
            outcome = action(issue)
        except Exception as failure:
            logger.exception("Nuke step %s failed for %s", name, issue.id)
            return StepResult(name, False, str(failure)[:300])
        if isinstance(outcome, StepResult):
            return outcome
        return StepResult(name, True, outcome)

    def _end_builder(self, issue: Issue) -> str:
        had = self._sessions.get(issue.id) is not None or issue.id in self._sessions.queued_ids()
        self._sessions.nuke(issue.id)
        team = self._settings.team_for(issue.team)
        number = self._store.numbers.pop(issue.id, None)
        if team and number is not None:
            self._store.drop(f"{team.github_repo}#{number}")
        self._store.save()
        return "builder ended and forgotten" if had else "no builder to end"

    def _close_pr(self, issue: Issue) -> StepResult:
        team = self._settings.team_for(issue.team)
        if self._settings.demo:
            return StepResult("pr", True, "demo: not touching GitHub", skipped=True)
        if team is None:
            return StepResult("pr", True, f"no repo mapped for team {issue.team}", skipped=True)
        pr = self._find_pr(team.github_repo, issue.branch)
        if pr is None:
            return StepResult("pr", True, "no PR", skipped=True)
        if pr.state != "OPEN":
            return StepResult(
                "pr", True, f"PR #{pr.number} already {pr.state.lower()}", skipped=True
            )
        self._close(team.github_repo, pr.number)
        return StepResult("pr", True, f"closed PR #{pr.number}")

    def _keep_assignment(self, issue: Issue) -> StepResult:
        return StepResult("linear", True, "assignment kept", skipped=True)

    def _unassign_issue(self, issue: Issue) -> StepResult:
        if self._settings.demo:
            return StepResult("linear", True, "demo: not touching Linear", skipped=True)
        if not self._settings.linear_api_key:
            return StepResult("linear", False, "no Linear API key")
        self._unassign(issue.id)
        return StepResult("linear", True, "unassigned")

    def _remove_worktree(self, issue: Issue) -> StepResult:
        team = self._settings.team_for(issue.team)
        if self._settings.demo:
            return StepResult("worktree", True, "demo: not touching the worktree", skipped=True)
        if team is None:
            return StepResult(
                "worktree", True, f"no repo mapped for team {issue.team}", skipped=True
            )
        if not team.worktree_path(issue.id).exists():
            return StepResult("worktree", True, "no worktree", skipped=True)
        path = verified_worktree(team, issue)
        notes: list[str] = []
        down = self._run(
            ["just", "worktree-down"],
            cwd=str(path),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        notes.append("containers down" if down.returncode == 0 else "worktree-down failed")
        remove = self._run(
            ["just", "worktree-remove", issue.branch],
            cwd=str(team.repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        notes.append("worktree-remove ok" if remove.returncode == 0 else "worktree-remove failed")
        if path.exists():
            forced = self._git(team.repo_path, "worktree", "remove", "--force", str(path))
            if forced.returncode != 0:
                raise RuntimeError(f"git worktree remove failed: {forced.stderr.strip()[-200:]}")
            notes.append("git worktree remove --force")
        self._git(team.repo_path, "worktree", "prune")
        branch = self._git(team.repo_path, "branch", "-d", issue.branch)
        notes.append(
            "merged branch deleted" if branch.returncode == 0 else "local branch kept (not merged)"
        )
        return StepResult("worktree", True, "; ".join(notes))

    def _forget(self, issue: Issue) -> str:
        self._board.forget(issue.id)
        return "row removed"
