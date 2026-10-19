"""Polls the base branch of each stacked builder and applies the rebase decision. Off unless AUTO_REBASE is set."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from cyclecontrol.config import Settings
from cyclecontrol.github import GithubError, find_pr_any_state
from cyclecontrol.rebase import RebaseAction, check_rebase, decide
from cyclecontrol.sessions import Session, SessionManager, notify
from cyclecontrol.worktrees import remote_sha

logger = logging.getLogger(__name__)


def write_base_file(session: Session) -> None:
    (session.git_dir / "spp-base").write_text(f"origin/{session.base}\n")


def _fetch(repo: Path, *branches: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "-q", "origin", *[b for b in branches if b]],
        check=False,
        timeout=120,
    )


class RebaseWatcher:
    def __init__(self, settings: Settings, sessions: SessionManager) -> None:
        self._settings = settings
        self._sessions = sessions

    def poll(self) -> list[tuple[str, RebaseAction]]:
        done: list[tuple[str, RebaseAction]] = []
        for session in self._sessions.all():
            if not session.pty.is_alive():
                continue
            action = self._one(session)
            if action.kind != "none":
                done.append((session.issue.id, action))
        return done

    def _one(self, session: Session) -> RebaseAction:
        team = self._settings.team_for(session.issue.team)
        if team is None or not session.base or session.base == team.base_branch:
            return RebaseAction("none")
        repo = team.repo_path
        base_tip = remote_sha(repo, session.base)
        default_tip = remote_sha(repo, team.base_branch)
        try:
            pr = find_pr_any_state(team.github_repo, session.base)
        except GithubError:
            pr = None
        merged = bool(pr and pr.state == "MERGED")
        if not (merged or (base_tip and base_tip != session.base_sha)):
            return RebaseAction("none")
        _fetch(repo, session.base, team.base_branch)
        action = decide(
            session.base,
            session.base_sha,
            team.base_branch,
            base_tip,
            merged,
            default_tip,
            lambda old, new: check_rebase(repo, session.issue.branch, old, new),
        )
        if action.kind == "conflict":
            self._conflict(session, action)
        elif action.kind == "rebase":
            self._rebase(session, action)
        return action

    def _conflict(self, session: Session, action: RebaseAction) -> None:
        key = (action.new_base, action.new_sha)
        if session.conflict_key == key:
            return
        session.conflict_key = key
        files = ", ".join(action.files) or action.text or "see the worktree"
        session.attention = f"Rebase conflict onto {action.new_base}: {files}"
        notify(f"{session.issue.id} needs you", session.attention)

    def _rebase(self, session: Session, action: RebaseAction) -> None:
        session.send(text=action.text)
        session.base, session.base_sha = action.new_base, action.new_sha
        session.attention, session.conflict_key = "", ()
        write_base_file(session)
        self._sessions.save()
