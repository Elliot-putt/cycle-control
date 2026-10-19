"""Builder sessions: one claude process per issue, each in its own worktree."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cyclecontrol import locks
from cyclecontrol.activity import (
    TranscriptReader,
    newest_transcript,
    project_dir,
    span,
    write_demo_transcript,
)
from cyclecontrol.config import Settings, TeamConfig
from cyclecontrol.models import Health, Issue, Mode, Progress
from cyclecontrol.progress import read_progress
from cyclecontrol.pty import PtySession, latest_line
from cyclecontrol.worktrees import (
    ensure_worktree,
    ensure_worktree_for_branch,
    git_dir,
    needs_provision,
    provision,
    remote_sha,
)

logger = logging.getLogger(__name__)

REMIND_AFTER_SECONDS = 300
SPECIAL_KEYS = {"enter": b"\r", "esc": b"\x1b"}

LANES: dict[Mode, str] = {
    "plan": "Plan-first: stop after your plan, show it to Elliot and wait for his approval before the breakdown.",
    "auto": "Autopilot: carry on through every phase and stop when the PR is green.",
}


def health_of(is_alive: bool, phase: str, idle_seconds: float, waiting_after: float) -> Health:
    """What the dashboard shows as the row's dot."""
    if not is_alive:
        return "done"
    if phase == "human":
        return "waiting"
    return "waiting" if idle_seconds >= waiting_after else "working"


class UnmappedTeamError(RuntimeError):
    pass


def builder_prompt(issue: Issue, worktree: Path, mode: Mode, base: str) -> str:
    return (
        f"Build Linear issue {issue.id}: {issue.title}\n{issue.url}\n"
        f"You are in the worktree {worktree} on branch {issue.branch}. "
        f"Base branch: {base}. Branch from origin/{base}, rebase onto it and open your PR with --base {base}. "
        f"{LANES[mode]}"
    )


def builder_argv(
    settings: Settings, team: TeamConfig, prompt: str, resume: bool, resume_id: str = ""
) -> list[str]:
    argv = [settings.claude_bin, "--agent", team.agent]
    if settings.skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if settings.claude_model:
        argv += ["--model", settings.claude_model]
    if resume:
        argv += ["--resume", resume_id] if resume_id else ["--continue"]
    else:
        argv.append(prompt)
    return argv


@dataclass
class Session:
    issue: Issue
    mode: Mode
    worktree: Path
    git_dir: Path
    pty: PtySession
    started_at: float = field(default_factory=time.time)
    was_waiting: bool = False
    waiting_since: float | None = None
    label_done: bool = False
    slack_link: str = ""
    approved: bool = False
    reminded: bool = False
    stopped: bool = False
    base: str = ""
    base_sha: str = ""
    attention: str = ""
    conflict_key: tuple = ()
    transcript_home: Path | None = None
    _reader: TranscriptReader | None = None

    def reader(self) -> TranscriptReader | None:
        path = newest_transcript(self.worktree, self.transcript_home)
        if path is None:
            return None
        if self._reader is None or self._reader.path != path:
            self._reader = TranscriptReader(path)
        self._reader.refresh()
        return self._reader

    def outcome(self, waiting_after: float) -> str:
        if self.attention and self.pty.is_alive():
            return "waiting"
        health = self.health(waiting_after)
        if health == "done" and not self.stopped and self.pty.exit_status() not in (0, None):
            return "failed"
        return health

    def activity(self, waiting_after: float) -> dict:
        reader = self.reader()
        if reader is None:
            return {"feed": [], "question": None}
        waiting = self.health(waiting_after) == "waiting"
        return {"feed": reader.feed(), "question": reader.question(waiting)}

    def send(self, text: str = "", key: str = "") -> None:
        if key:
            self.pty.write(SPECIAL_KEYS.get(key, key[:1].encode()))
            return
        if text.strip():
            self.pty.write(text.encode())
            threading.Timer(0.2, self.pty.write, args=(b"\r",)).start()

    def live_activity(self) -> str:
        reader = self.reader()
        tool = reader.latest_tool() if reader else ""
        if not tool:
            return ""
        age = max(0, int(time.time() - self.pty.last_output))
        return f"{tool} · {span(age)}"[:70]

    def progress(self) -> Progress:
        return read_progress(self.git_dir)

    def health(self, waiting_after: float) -> Health:
        return health_of(
            self.pty.is_alive(),
            self.progress().phase,
            time.time() - self.pty.last_output,
            waiting_after,
        )

    def summary(self, waiting_after: float) -> dict:
        progress = self.progress()
        return {
            "id": self.issue.id,
            "title": self.issue.title,
            "url": self.issue.url,
            "mode": self.mode,
            "health": self.health(waiting_after),
            "outcome": self.outcome(waiting_after),
            "activity": self.live_activity(),
            "phase": progress.phase,
            "note": progress.note,
            "summary": progress.summary,
            "plan": [step.__dict__ for step in progress.plan],
            "gateBlocks": progress.gate_blocks,
            "gateWarns": progress.gate_warns,
            "reviewFindings": progress.review_findings,
            "gates": progress.gates,
            "review": progress.review,
            "prUrl": progress.pr_url,
            "approved": self.approved,
            "slackLink": self.slack_link,
            "base": self.base,
            "attention": self.attention,
            "latest": latest_line(self.pty.tail()),
            "worktree": str(self.worktree),
        }


def read_registry(path: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"sessions": [], "queue": []}
    if isinstance(data, list):
        return {"sessions": [e for e in data if isinstance(e, dict)], "queue": []}
    if not isinstance(data, dict):
        return {"sessions": [], "queue": []}
    return {
        "sessions": [e for e in data.get("sessions", []) if isinstance(e, dict)],
        "queue": [e for e in data.get("queue", []) if isinstance(e, dict)],
    }


def write_registry(path: Path, sessions: list[dict], queue: list[dict] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": sessions, "queue": queue or []}, indent=2) + "\n")


def flags_of(session: Session) -> dict:
    return {
        "labelDone": session.label_done,
        "slackLink": session.slack_link,
        "approved": session.approved,
    }


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, Session] = {}
        self._queue: list[dict] = []
        self.errors: dict[str, str] = {}

    def get(self, issue_id: str) -> Session | None:
        return self._sessions.get(issue_id)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def _team(self, issue: Issue) -> TeamConfig:
        team = self._settings.team_for(issue.team)
        if team is None:
            raise UnmappedTeamError(f"No repo mapped for team {issue.team}.")
        return team

    def clear_attention(self, issue_id: str) -> None:
        """You resolved the conflict yourself: take the conflicting target as the new base."""
        session = self._sessions.get(issue_id)
        if session is None:
            return
        if session.conflict_key:
            session.base, session.base_sha = session.conflict_key
            self._write_base(session)
        session.attention, session.conflict_key = "", ()
        self._persist()

    @staticmethod
    def _write_base(session: Session) -> None:
        try:
            (session.git_dir / "spp-base").write_text(f"origin/{session.base}\n")
        except OSError:
            logger.warning("Could not write the base file for %s", session.issue.id)

    def counted(self) -> list[Session]:
        """Builders that hold containers: alive and not waiting for review at the human phase."""
        return [
            s for s in self._sessions.values() if s.pty.is_alive() and s.progress().phase != "human"
        ]

    def queued_ids(self) -> set[str]:
        return {entry["issue"]["id"] for entry in self._queue}

    def queue_view(self) -> list[dict]:
        return [
            {
                "id": e["issue"]["id"],
                "title": e["issue"].get("title", ""),
                "base": e.get("base", ""),
                "position": i + 1,
            }
            for i, e in enumerate(self._queue)
        ]

    def request_start(
        self,
        issue: Issue,
        mode: Mode,
        base: str = "",
        resume: bool = False,
        flags: dict | None = None,
        resume_id: str = "",
        prompt: str = "",
    ) -> str:
        """Start now if a slot is free, else queue. Returns started, queued or running."""
        existing = self._sessions.get(issue.id)
        if existing and existing.pty.is_alive():
            return "running"
        if issue.id in self.queued_ids():
            return "queued"
        team = self._team(issue)
        if len(self.counted()) < self._settings.max_builders:
            self.start(
                issue,
                mode,
                resume=resume,
                base=base,
                flags=flags,
                resume_id=resume_id,
                prompt=prompt,
            )
            return "started"
        self._queue.append(
            {
                "issue": asdict(issue),
                "mode": mode,
                "base": base or team.base_branch,
                "resume": resume,
                "flags": flags or {},
                "resumeId": resume_id,
                "prompt": prompt,
            }
        )
        self._persist()
        return "queued"

    def revive_for_comments(self, issue: Issue, mode: Mode, base: str, branch: str) -> str:
        """Bring an ended builder back to handle review feedback: worktree, provisioning, then resume."""
        team = self._team(issue)
        worktree = ensure_worktree_for_branch(team, issue, branch)
        if needs_provision(worktree):
            provision(worktree)
        found = newest_transcript(worktree)
        return self.request_start(
            issue, mode, base=base, resume=True, resume_id=found.stem if found else ""
        )

    def start_fresh_for_pr(self, issue: Issue, mode: Mode, base: str, prompt: str) -> str:
        """A new builder in the worktree when the old conversation cannot be resumed."""
        return self.request_start(issue, mode, base=base, prompt=prompt)

    def drain(self) -> list[str]:
        """Start queued builders while slots are free."""
        started: list[str] = []
        changed = False
        while self._queue and len(self.counted()) < self._settings.max_builders:
            entry = self._queue.pop(0)
            changed = True
            issue = Issue.from_dict(entry["issue"])
            try:
                self.start(
                    issue,
                    entry["mode"],
                    resume=entry.get("resume", False),
                    base=entry.get("base", ""),
                    flags=entry.get("flags"),
                    resume_id=entry.get("resumeId", ""),
                    prompt=entry.get("prompt", ""),
                )
                started.append(issue.id)
            except Exception as failure:
                logger.exception("Could not start queued %s", issue.id)
                self.errors[issue.id] = str(failure)
        if changed:
            self._persist()
        return started

    def cancel_queued(self, issue_id: str) -> None:
        self._queue = [e for e in self._queue if e["issue"]["id"] != issue_id]
        self._persist()

    def start(
        self,
        issue: Issue,
        mode: Mode,
        resume: bool = False,
        base: str = "",
        flags: dict | None = None,
        resume_id: str = "",
        prompt: str = "",
    ) -> Session:
        existing = self._sessions.get(issue.id)
        if existing and existing.pty.is_alive():
            return existing
        team = self._team(issue)
        base = base or team.base_branch
        if self._settings.demo:
            return self._start_demo(issue, mode, base)
        worktree = ensure_worktree(team, issue, base)
        prompt = prompt or builder_prompt(issue, worktree, mode, base)
        gitdir = git_dir(worktree)
        locks.acquire(gitdir, worktree, issue.id)
        pty = PtySession(
            builder_argv(self._settings, team, prompt, resume, resume_id),
            str(worktree),
            env={"SPP_BASE": f"origin/{base}"},
        )
        try:
            pty.start()
        except BaseException:
            locks.release(gitdir)
            raise
        locks.set_pid(gitdir, pty.pid)
        session = Session(
            issue,
            mode,
            worktree,
            gitdir,
            pty,
            base=base,
            base_sha=remote_sha(team.repo_path, base),
        )
        self._apply_flags(session, flags)
        self._write_base(session)
        self._sessions[issue.id] = session
        self.errors.pop(issue.id, None)
        self._persist()
        return session

    @staticmethod
    def _apply_flags(session: Session, flags: dict | None) -> None:
        if not flags:
            return
        session.label_done = bool(flags.get("labelDone"))
        session.slack_link = str(flags.get("slackLink", ""))
        session.approved = bool(flags.get("approved"))

    def _persist(self) -> None:
        if self._settings.demo:
            return
        sessions = [
            {
                "issue": asdict(s.issue),
                "mode": s.mode,
                "base": s.base,
                "baseSha": s.base_sha,
                **flags_of(s),
            }
            for s in self._sessions.values()
            if not s.stopped
        ]
        write_registry(self._settings.state_path, sessions, self._queue)

    def restore(self) -> list[str]:
        """Resume every builder that was running when the app last closed, and keep the queue."""
        if self._settings.demo:
            return []
        saved = read_registry(self._settings.state_path)
        self._queue = list(saved["queue"])
        resumed: list[str] = []
        for entry in saved["sessions"]:
            try:
                issue = Issue.from_dict(entry["issue"])
                outcome = self.request_start(
                    issue,
                    entry.get("mode", "plan"),
                    base=entry.get("base", ""),
                    resume=True,
                    flags=entry,
                )
            except Exception:
                logger.exception("Could not resume %s", entry.get("issue", {}).get("id"))
                continue
            session = self._sessions.get(issue.id)
            if outcome == "started" and session:
                session.was_waiting = True
                session.base_sha = entry.get("baseSha", session.base_sha)
            resumed.append(issue.id)
        return resumed

    def _start_demo(self, issue: Issue, mode: Mode, base: str) -> Session:
        worktree = Path(tempfile.mkdtemp(prefix=f"demo-{issue.id}-"))
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        gitdir = git_dir(worktree)
        phase = (
            '{"phase":"human","note":"https://github.com/AgentSoftware/street-partner-platform/pull/1479"}'
            if mode == "auto"
            else '{"phase":"plan","note":"drafting the plan"}'
        )
        (gitdir / "spp-phase.json").write_text(phase)
        (gitdir / "spp-plan.json").write_text(
            '{"summary":"Swap dayjs for date-fns in 10 files","steps":['
            '{"id":"s1","label":"Add date-fns helpers","kind":"frontend"},'
            '{"id":"s2","label":"Port the calendar composable","kind":"frontend"},'
            '{"id":"s3","label":"Drop dayjs and its plugins","kind":"config"}]}'
        )
        script = (
            f"echo 'Demo builder for {issue.id} ({mode})'; echo 'Plan written. Approve it? (y/n)'; "
            'read answer; echo "You said: $answer"; sleep 600'
        )
        pty = PtySession(["bash", "-c", script], str(worktree))
        pty.start()
        home = Path(tempfile.mkdtemp(prefix="demo-home-"))
        write_demo_transcript(project_dir(worktree, home))
        session = Session(issue, mode, worktree, gitdir, pty, base=base, transcript_home=home)
        self._sessions[issue.id] = session
        return session

    def save(self) -> None:
        self._persist()

    def stop(self, issue_id: str) -> None:
        session = self._sessions.get(issue_id)
        if session:
            session.stopped = True
            session.pty.terminate()
            locks.release(session.git_dir, session.pty.pid)
        self._queue = [e for e in self._queue if e["issue"]["id"] != issue_id]
        self._persist()

    def stop_all(self) -> None:
        for session in self._sessions.values():
            session.pty.terminate()
            locks.release(session.git_dir, session.pty.pid)

    def worktree_free(self, session: Session) -> tuple[bool, str]:
        """Whether the worktree's old builder is really gone, checked against the lock and the process table."""
        return locks.confirm_gone(session.git_dir, session.worktree)

    def release_dead(self) -> None:
        """Drop the locks of builders that have ended."""
        for session in self._sessions.values():
            if not session.pty.is_alive():
                locks.release(session.git_dir, session.pty.pid)

    def check_waiting(self, now: float | None = None) -> tuple[list[Session], list[Session]]:
        """Sessions that just started waiting on you, and ones due their single 5-minute reminder."""
        now = time.time() if now is None else now
        waiting = self._settings.waiting_after_seconds
        fresh: list[Session] = []
        reminders: list[Session] = []
        for session in self._sessions.values():
            is_waiting = session.health(waiting) == "waiting"
            if not is_waiting:
                session.waiting_since = None
                session.reminded = False
            else:
                if session.waiting_since is None:
                    session.waiting_since = now
                if not session.was_waiting:
                    fresh.append(session)
                elif not session.reminded and now - session.waiting_since >= REMIND_AFTER_SECONDS:
                    session.reminded = True
                    reminders.append(session)
            session.was_waiting = is_waiting
        return fresh, reminders


def notify(title: str, message: str) -> None:
    if os.environ.get("CYCLE_NO_NOTIFY"):
        return
    safe = message.replace('"', "'")[:200]
    script = f'display notification "{safe}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not send a notification")
