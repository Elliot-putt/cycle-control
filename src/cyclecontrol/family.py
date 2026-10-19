"""Parents with sub-issues: their PR state, and which children are ready to start."""

from __future__ import annotations

import json
import logging
from typing import Any

from cyclecontrol.board import Board
from cyclecontrol.config import Settings, TeamConfig
from cyclecontrol.github import GithubError, find_pr_any_state
from cyclecontrol.models import Issue
from cyclecontrol.sessions import SessionManager
from cyclecontrol.stacks import (
    ChildState,
    PrInfo,
    StackEntry,
    branches_to_check,
    read_plan_stack,
    resolve_stack,
)
from cyclecontrol.worktrees import WorktreeError, git_dir

logger = logging.getLogger(__name__)


class FamilyService:
    def __init__(self, settings: Settings, board: Board, sessions: SessionManager) -> None:
        self._settings = settings
        self._board = board
        self._sessions = sessions
        self._prs: dict[str, PrInfo | None] = {}
        self._plans: dict[str, list[StackEntry]] = {}

    def _read_plan(self, team: TeamConfig, parent: Issue) -> list[StackEntry]:
        try:
            plan = json.loads(
                (git_dir(team.worktree_path(parent.id)) / "spp-plan.json").read_text()
            )
        except (OSError, ValueError, WorktreeError):
            return self._plans.get(parent.id, [])
        return read_plan_stack(plan)

    def refresh(self) -> None:
        """Look up the PR of every child's branch. Slow, so run it off the request path."""
        if self._settings.demo:
            return
        for parent in self._board.parents():
            team = self._settings.team_for(parent.team)
            if team is None:
                continue
            children = self._board.children_of(parent.id)
            plan = self._read_plan(team, parent)
            self._plans[parent.id] = plan
            for child_id, branch in branches_to_check(children, plan).items():
                try:
                    found = find_pr_any_state(team.github_repo, branch)
                except GithubError:
                    logger.warning("Could not look up the PR for %s", branch)
                    continue
                self._prs[child_id] = (
                    PrInfo(
                        found.number,
                        found.url,
                        found.branch,
                        found.state,
                        found.base,
                        found.head_sha,
                    )
                    if found
                    else None
                )

    def resolve(self, parent: Issue) -> list[ChildState]:
        team = self._settings.team_for(parent.team)
        default_base = team.base_branch if team else "develop"
        running = {s.issue.id for s in self._sessions.all() if s.pty.is_alive()}
        return resolve_stack(
            self._board.children_of(parent.id),
            self._prs,
            running,
            self._sessions.queued_ids(),
            default_base,
            self._plans.get(parent.id, []),
        )

    def groups(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for parent in self._board.parents():
            team = self._settings.team_for(parent.team)
            by_id = {c.id: c for c in self._board.children_of(parent.id)}
            states = self.resolve(parent)
            children = []
            for state in states:
                child = by_id[state.issue_id]
                reason = ""
                if team is None:
                    reason = f"No repo mapped for team {child.team}."
                elif state.kind == "blocked":
                    reason = "Waiting on " + ", ".join(state.waiting_on) + "."
                children.append(
                    {
                        "id": child.id,
                        "title": child.title,
                        "url": child.url,
                        "status": child.status,
                        "kind": state.kind,
                        "base": state.base,
                        "waitingOn": list(state.waiting_on),
                        "prNumber": state.pr.number if state.pr else 0,
                        "prUrl": state.pr.url if state.pr else "",
                        "startable": state.kind == "ready" and team is not None,
                        "startReason": reason,
                    }
                )
            out.append(
                {
                    "id": parent.id,
                    "title": parent.title,
                    "url": parent.url,
                    "status": parent.status,
                    "isMine": parent.is_mine or any(by_id[c["id"]].is_mine for c in children),
                    "teamKey": parent.team,
                    "children": children,
                    "readyCount": sum(
                        1 for c in children if c["kind"] == "ready" and c["startable"]
                    ),
                    "doneCount": sum(1 for c in children if c["kind"] in ("done", "pr_open")),
                    "startReason": "" if team else f"No repo mapped for team {parent.team}.",
                }
            )
        return out
