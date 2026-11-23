"""Builder sessions: one claude process per issue, each in its own worktree."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cyclecontrol import journal, locks
from cyclecontrol.activity import (
    TranscriptReader,
    newest_transcript,
    project_dir,
    span,
    write_demo_transcript,
)
from cyclecontrol.busy import WATCHER
from cyclecontrol.config import Settings, TeamConfig
from cyclecontrol.models import Health, Issue, Mode, Progress
from cyclecontrol.progress import progress_stamp, read_progress
from cyclecontrol.pty import PtySession, SleepingPty, latest_line
from cyclecontrol.worktrees import (
    WorktreeError,
    ensure_worktree,
    ensure_worktree_for_branch,
    git_dir,
    needs_provision,
    provision,
    remote_sha,
)

logger = logging.getLogger(__name__)

REMIND_AFTER_SECONDS = 300
NUDGE_AFTER_SECONDS = 20
NUDGE_WINDOW = 1500
MAX_NUDGES = 3
FORGET_FINISHED_AFTER_SECONDS = 14 * 86400
CUT_OFF = re.compile(
    r"API Error: ?[^\n]*(went to sleep mid-response|may be incomplete|timed out|connection error|"
    r"network|ECONNRESET|fetch failed|overloaded|529|5\d\d)",
    re.IGNORECASE,
)
CUT_OFF_NOTE = (
    "Your last response was cut off by an API error, most likely because the Mac slept. "
    "Nothing else has changed. Carry on from where you left off: check what you last did "
    "(git status, the PR, any half-finished edit) and finish it, unless you were waiting on Elliot's answer."
)
SPECIAL_KEYS = {"enter": b"\r", "esc": b"\x1b"}

LANES: dict[Mode, str] = {
    "plan": "Plan-first: stop after your plan, show it to Elliot and wait for his approval before the breakdown.",
    "auto": "Autopilot: carry on through every phase and stop when the PR is green.",
}


def health_of(
    is_alive: bool, phase: str, idle_seconds: float, waiting_after: float, busy: bool = False
) -> Health:
    """What the dashboard shows as the row's dot."""
    if not is_alive:
        return "done"
    if phase == "human":
        return "waiting"
    return "waiting" if idle_seconds >= waiting_after and not busy else "working"


LOST_CONVERSATION = (
    "Your earlier conversation for this issue could not be found, so you are starting fresh in a worktree that may already hold work. "
    "Before anything else read the git dir's spp-phase.json and spp-plan.json, `git log` and `git status`, and any branches or worktrees the "
    "plan lists. Carry on from where the work stands; do not redo finished work.\n\n"
)


CI_GREEN_NOTE = (
    "CI is green on PR #{number} and you are still in the ci phase. Finish the step now: run `just worktree-down`, "
    're-read the PR\'s comments and reviews once more, handle anything new, then `phase.sh human "<PR url>"` '
    "listing every PR of the stack, so Elliot can approve. Do not wait for anything else."
)

MEMORY_NUDGE_AFTER_SECONDS = 600
MAX_MEMORY_NUDGES = 3
MEMORY_KILL_NOTE = (
    "About {minutes} minutes ago {step} was killed because the Mac was critically low on memory. Memory has had "
    "time to recover, and Elliot asks you to retry now: retry the smallest failed step once, one container job "
    "at a time (no parallel docker or compose jobs, nothing else heavy running alongside). "
    "If it is killed again, stop and say so; do not retry a second time."
)
PROCESS_KILLED = "the builder process itself"

RESUME_NOTE = (
    "Cycle Control was restarted and your session was reopened. Carry on from where you left off: "
    "check the phase and what you were doing, and continue unless you were waiting on Elliot's answer."
)


def resume_choice(worktree: Path, resume: bool, resume_id: str) -> tuple[bool, str, bool]:
    """Resume only when there is a conversation to resume. Returns (resume, resume_id, conversation_lost)."""
    if not resume or resume_id:
        return resume, resume_id, False
    if newest_transcript(worktree) is None:
        return False, "", True
    return True, "", False


PR_OPEN_PHASES = ("pr", "ci", "human")
ANSI_CODES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
CURSOR_FORWARD = re.compile(r"\x1b\[(\d*)C")


def screen_text(raw: bytes) -> str:
    """The terminal bytes as plain text. Claude Code draws runs of spaces as cursor-forward codes,
    so those become spaces first, or words run together and nothing matches."""
    text = raw.decode("utf-8", errors="replace")
    text = CURSOR_FORWARD.sub(lambda m: " " * int(m.group(1) or 1), text)
    return ANSI_CODES.sub("", text)


PR_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class UnmappedTeamError(RuntimeError):
    pass


class NotYoursError(RuntimeError):
    """The issue is assigned to someone else; a builder only starts on it when told to."""


def builder_prompt(issue: Issue, worktree: Path, mode: Mode, base: str) -> str:
    return (
        f"Build Linear issue {issue.id}: {issue.title}\n{issue.url}\n"
        f"You are in the worktree {worktree} on branch {issue.branch}. "
        f"Base branch: {base}. Branch from origin/{base}, rebase onto it and open your PR with --base {base}. "
        f"{LANES[mode]}"
    )


def builder_argv(
    settings: Settings,
    team: TeamConfig,
    prompt: str,
    resume: bool,
    resume_id: str = "",
    resume_note: str = "",
) -> list[str]:
    argv = [settings.claude_bin, "--agent", team.agent]
    if settings.skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if settings.claude_model:
        argv += ["--model", settings.claude_model]
    if resume:
        argv += ["--resume", resume_id] if resume_id else ["--continue"]
        if resume_note:
            argv.append(resume_note)
    else:
        argv.append(prompt)
    return argv


def drop_truncated(links: list[str]) -> list[str]:
    """Drop a link that is a prefix of another (a wrapped line cut pull/36269 down to pull/36)."""
    return [u for u in links if not any(o != u and o.startswith(u) for o in links)]


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
    labelled: list[str] = field(default_factory=list)
    slack_links: dict[str, str] = field(default_factory=dict)
    approved: bool = False
    reminded: bool = False
    stopped: bool = False
    base: str = ""
    base_sha: str = ""
    attention: str = ""
    conflict_key: tuple = ()
    transcript_home: Path | None = None
    asleep: bool = False
    finished: bool = False
    finished_at: float = 0.0
    nudged_at: float = 0.0
    nudges: int = 0
    ci_nudged_at: float = 0.0
    ci_nudges: int = 0
    ci_escalated: bool = False
    memory_kill_handled: str = ""
    memory_nudges: int = 0
    memory_nudge_phase: str = ""
    memory_escalated: bool = False
    cut_off_logged_at: float | None = None
    _reader: TranscriptReader | None = None
    _progress: tuple[tuple, Progress] | None = None

    def reader(self) -> TranscriptReader | None:
        path = newest_transcript(self.worktree, self.transcript_home)
        if path is None:
            return None
        if self._reader is None or self._reader.path != path:
            self._reader = TranscriptReader(path)
        self._reader.refresh()
        return self._reader

    def outcome(self, waiting_after: float) -> str:
        if not self.pty.is_alive():
            if self.finished:
                return "finished"
            if self.asleep:
                return "sleeping"
        if self.attention and self.pty.is_alive():
            return "waiting"
        health = self.health(waiting_after)
        if health == "done" and not self.stopped:
            return "failed" if self.pty.exit_status() not in (0, None) else "ended"
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

    def pr_url_from_chat(self) -> str:
        """The PR link the builder last printed, for builders that opened one without moving to the PR phase."""
        links = self.pr_urls_from_chat()
        return links[-1] if links else ""

    def pr_urls_from_chat(self) -> list[str]:
        """Every distinct PR link the builder printed, in the order it first printed them: a stack prints several."""
        text = screen_text(self.pty.tail())
        links = list(dict.fromkeys(PR_LINK.findall(text)))
        # A link the terminal wrapped mid-number reads as a prefix of the real one (pull/36 for pull/36269).
        return drop_truncated(links)

    def pr_urls(self) -> list[str]:
        links = self.pr_urls_from_chat()
        recorded = [u for u in PR_LINK.findall(self.progress().pr_url) if u not in links]
        return drop_truncated(links + recorded)

    def live_activity(self) -> str:
        if not self.pty.is_alive():
            return ""
        reader = self.reader()
        tool = reader.latest_tool() if reader else ""
        if not tool:
            return ""
        age = max(0, int(time.time() - self.pty.last_output))
        return f"{tool} · {span(age)}"[:70]

    def progress(self) -> Progress:
        stamp = progress_stamp(self.git_dir)
        if self._progress is None or self._progress[0] != stamp:
            self._progress = (stamp, read_progress(self.git_dir))
        return self._progress[1]

    def health(self, waiting_after: float) -> Health:
        idle = time.time() - self.pty.last_output
        return health_of(
            self.pty.is_alive(),
            self.progress().phase,
            idle,
            waiting_after,
            idle >= waiting_after and WATCHER.running(getattr(self.pty, "pid", 0)),
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
            "prUrl": progress.pr_url or self.pr_url_from_chat(),
            "approved": self.approved,
            "slackLink": self.slack_link,
            "slackLinks": self.slack_links,
            "base": self.base,
            "attention": self.attention,
            "sleeping": self.asleep and not self.pty.is_alive(),
            "finished": self.finished,
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
        "labelled": session.labelled,
        "slackLinks": session.slack_links,
        "approved": session.approved,
        "finished": session.finished,
        "finishedAt": session.finished_at,
        "memoryKillHandled": session.memory_kill_handled,
        "memoryNudges": session.memory_nudges,
        "memoryNudgePhase": session.memory_nudge_phase,
        "memoryEscalated": session.memory_escalated,
    }


def memory_kill_of(session: Session) -> dict[str, str] | None:
    """The low-memory kill that ended the builder's last turn, as {key, step}, or None.

    The transcript is the reliable record: Claude Code writes a task notification when it reaps a background
    command for memory ("critically low on memory"), and a container job killed by the OOM killer comes back as
    "Exit code 137". A new prompt clears it, so only the turn the builder stopped on counts. A builder whose own
    process died of SIGKILL (exit 137 from a shell) was most likely killed by macOS for memory too."""
    reader = session.reader()
    kill = reader.memory_kill if reader else None
    if kill:
        return {"key": f"transcript:{kill['at']}:{kill['step']}", "step": kill["step"]}
    if session.pty.is_alive() or session.asleep:
        return None
    signal = getattr(session.pty, "exit_signal", lambda: None)()
    if signal == 9 or session.pty.exit_status() == 137:
        return {
            "key": f"process:{session.pty.pid}:{session.pty.last_output}",
            "step": PROCESS_KILLED,
        }
    return None


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, Session] = {}
        self._queue: list[dict] = []
        self.errors: dict[str, str] = {}
        self._nuked: set[str] = set()

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
        return [s for s in self.all() if s.pty.is_alive() and s.progress().phase != "human"]

    def live(self) -> list[Session]:
        """Every builder with a claude process, whatever its phase."""
        return [s for s in self.all() if s.pty.is_alive()]

    def slot_free(self) -> bool:
        return (
            len(self.counted()) < self._settings.max_builders
            and len(self.live()) < self._settings.max_live_builders
        )

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
        force: bool = False,
    ) -> str:
        """Start now if a slot is free, else queue. Returns started, queued or running."""
        if issue.id in self._nuked:
            return "nuked"
        existing = self._sessions.get(issue.id)
        if existing and existing.pty.is_alive():
            return "running"
        if issue.id in self.queued_ids():
            return "queued"
        if issue.assignee and not issue.is_mine and not (force or resume or existing):
            raise NotYoursError(f"{issue.id} is assigned to {issue.assignee}.")
        team = self._team(issue)
        if self.slot_free():
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
        while self._queue and self.slot_free():
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
        resume, resume_id, lost = resume_choice(worktree, resume, resume_id)
        resume_note = prompt if resume else ""
        prompt = prompt or builder_prompt(issue, worktree, mode, base)
        if lost:
            logger.warning("No conversation to resume for %s; starting fresh", issue.id)
            prompt = LOST_CONVERSATION + prompt
        gitdir = git_dir(worktree)
        locks.acquire(gitdir, worktree, issue.id)
        pty = PtySession(
            builder_argv(self._settings, team, prompt, resume, resume_id, resume_note),
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
        session.labelled = [str(url) for url in flags.get("labelled", [])]
        session.slack_links = {str(k): str(v) for k, v in flags.get("slackLinks", {}).items()}
        session.approved = bool(flags.get("approved"))
        session.finished = bool(flags.get("finished"))
        session.finished_at = float(flags.get("finishedAt") or 0)
        session.memory_kill_handled = str(flags.get("memoryKillHandled") or "")
        session.memory_nudges = int(flags.get("memoryNudges") or 0)
        session.memory_nudge_phase = str(flags.get("memoryNudgePhase") or "")
        session.memory_escalated = bool(flags.get("memoryEscalated"))
        if session.finished and not session.finished_at:
            session.finished_at = time.time()

    def _persist(self) -> None:
        if self._settings.demo:
            return
        sessions = [
            {
                "issue": asdict(s.issue),
                "mode": s.mode,
                "base": s.base,
                "baseSha": s.base_sha,
                "asleep": s.asleep and not s.pty.is_alive(),
                "waiting": s.was_waiting,
                **flags_of(s),
            }
            for s in self.all()
            if not s.stopped
        ]
        write_registry(self._settings.state_path, sessions, self._queue)

    def wake(self, issue_id: str, prompt: str = "") -> str:
        """Bring a sleeping builder back with its conversation, and give it the prompt if there is one.

        Returns started, queued or running. A running builder is given the prompt directly.
        """
        session = self._sessions.get(issue_id)
        if session is None:
            return "unknown"
        if session.pty.is_alive():
            if prompt:
                session.send(text=prompt)
            return "running"
        killed = locks.end_own(session.git_dir)
        if killed:
            logger.warning(
                "Ended leftover builder pid %s for %s before waking it", killed, issue_id
            )
        found = newest_transcript(session.worktree, session.transcript_home)
        return self.request_start(
            session.issue,
            session.mode,
            base=session.base,
            resume=True,
            flags=flags_of(session),
            resume_id=found.stem if found else "",
            prompt=prompt,
        )

    def sleep_idle(self, now: float | None = None) -> list[Session]:
        """End builders that are only waiting, freeing their memory: once the PR is open (pr, ci, human) after
        PARK_AFTER_SECONDS, anywhere else after WAITING_CLOSE_SECONDS. Never one mid-work or with a conflict for you.
        At ci a background shell is only a CI wait the app does for it, so it does not block."""
        now = time.time() if now is None else now
        slept: list[Session] = []
        for session in self.all():
            if not session.pty.is_alive() or session.attention:
                continue
            phase = session.progress().phase
            pr_open = phase in PR_OPEN_PHASES
            limit = (
                self._settings.park_after_seconds
                if pr_open
                else self._settings.waiting_close_seconds
            )
            if limit <= 0 or now - session.pty.last_output < limit:
                continue
            if phase != "ci" and WATCHER.running(session.pty.pid):
                continue
            self._sleep(session)
            slept.append(session)
        if slept:
            self._persist()
        return slept

    def finish(self, issue_id: str) -> None:
        """The PR is merged or closed: end the builder for good. It keeps its row and never restarts on launch."""
        session = self._sessions.get(issue_id)
        if session is None or session.finished:
            return
        session.finished = True
        session.finished_at = time.time()
        if session.pty.is_alive():
            self._sleep(session)
        self._persist()

    def forget_finished(self, now: float | None = None) -> list[str]:
        """Drop finished builders from the panel two weeks after their PR merged or closed. The worktree stays."""
        now = time.time() if now is None else now
        old = [
            s.issue.id
            for s in self.all()
            if s.finished and s.finished_at and now - s.finished_at > FORGET_FINISHED_AFTER_SECONDS
        ]
        for issue_id in old:
            del self._sessions[issue_id]
        if old:
            self._persist()
        return old

    def _sleep(self, session: Session) -> None:
        pid = session.pty.pid
        session.pty.terminate()
        if pid > 0 and locks.PROCS.claude_running(pid):
            logger.warning(
                "Builder %s (pid %s) survived terminate; killing it", session.issue.id, pid
            )
            locks.PROCS.kill(pid)
        locks.release(session.git_dir, pid)
        session.pty = SleepingPty()
        session.asleep = True
        session._reader = None

    def _register_saved(self, entry: dict, issue: Issue) -> None:
        """Register a saved builder without a process, so its lane, comments and Wake still work."""
        team = self._team(issue)
        worktree = team.worktree_path(issue.id)
        session = Session(
            issue,
            entry.get("mode", "plan"),
            worktree,
            self._git_dir_or_default(worktree),
            SleepingPty(),
            base=entry.get("base", "") or team.base_branch,
            base_sha=entry.get("baseSha", ""),
            asleep=True,
            was_waiting=bool(entry.get("waiting")),
        )
        self._apply_flags(session, entry)
        self._sessions[issue.id] = session

    @staticmethod
    def _git_dir_or_default(worktree: Path) -> Path:
        try:
            return git_dir(worktree)
        except WorktreeError:
            return worktree / ".git"

    def _restore_kind(self, entry: dict, issue: Issue) -> str:
        """resume for a builder that was working, sleep for one that was only waiting, finished for a merged one."""
        if entry.get("finished"):
            return "finished"
        if self._settings.resume_all_on_launch:
            return "resume"
        if entry.get("asleep") or entry.get("waiting"):
            return "sleep"
        worktree = self._team(issue).worktree_path(issue.id)
        phase = read_progress(self._git_dir_or_default(worktree)).phase
        return "sleep" if phase == "human" else "resume"

    def restore(self) -> list[str]:
        """Resume the builders that were working when the app last closed; the rest come back asleep."""
        if self._settings.demo:
            return []
        saved = read_registry(self._settings.state_path)
        self._queue = list(saved["queue"])
        resumed: list[str] = []
        for entry in saved["sessions"]:
            try:
                issue = Issue.from_dict(entry["issue"])
                if self._restore_kind(entry, issue) != "resume":
                    self._register_saved(entry, issue)
                    continue
                outcome = self.request_start(
                    issue,
                    entry.get("mode", "plan"),
                    base=entry.get("base", ""),
                    resume=True,
                    flags=entry,
                    prompt=RESUME_NOTE,
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

    def nuke(self, issue_id: str) -> None:
        """Stop the builder, forget it, and refuse to ever start it again in this run."""
        self._nuked.add(issue_id)
        self.stop(issue_id)
        self._sessions.pop(issue_id, None)
        self.errors.pop(issue_id, None)
        self._persist()

    def stop_all(self) -> None:
        for session in self.all():
            session.pty.terminate()
            locks.release(session.git_dir, session.pty.pid)

    def worktree_free(self, session: Session) -> tuple[bool, str]:
        """Whether the worktree's old builder is really gone, checked against the lock and the process table."""
        return locks.confirm_gone(session.git_dir, session.worktree)

    def release_dead(self) -> None:
        """Drop the locks of builders that have ended."""
        for session in self.all():
            if not session.pty.is_alive() and not session.asleep:
                locks.release(session.git_dir, session.pty.pid)

    def unposted_pr_urls(self) -> dict[str, list[str]]:
        """PR links per live issue that the app has not seen in Slack: what a channel scan should look for."""
        wanted: dict[str, list[str]] = {}
        for session in self.all():
            if session.finished or session.stopped:
                continue
            urls = [u for u in session.pr_urls() if u not in session.slack_links]
            if urls:
                wanted[session.issue.id] = urls
        return wanted

    def mark_posted(self, found: dict[str, str]) -> list[tuple[Session, str, str]]:
        """Record the Slack messages Elliot posted himself, so Approve skips the post and the row says so."""
        marked: list[tuple[Session, str, str]] = []
        for issue_id, urls in self.unposted_pr_urls().items():
            session = self._sessions[issue_id]
            for url in urls:
                link = found.get(url)
                if not link:
                    continue
                session.slack_links[url] = link
                session.slack_link = session.slack_link or link
                marked.append((session, url, link))
        if marked:
            self._persist()
        return marked

    def nudge_cut_off(self, now: float | None = None) -> list[Session]:
        """Builders whose last turn died with an API error (the Mac slept mid-response) sit at the prompt
        forever. Tell each one to carry on, once per stall and at most MAX_NUDGES times."""
        now = time.time() if now is None else now
        nudged: list[Session] = []
        for session in self.all():
            if not session.pty.is_alive() or session.stopped or session.nudges >= MAX_NUDGES:
                continue
            if now - session.pty.last_output < NUDGE_AFTER_SECONDS:
                continue
            if session.nudged_at >= session.pty.last_output:
                continue
            text = screen_text(session.pty.tail())
            found = None
            for found in CUT_OFF.finditer(text):
                pass
            if found is None:
                continue
            distance = len(text) - found.end()
            if distance > NUDGE_WINDOW:
                self._log_skipped_cut_off(
                    session, f"the error is {distance} characters above the end of the screen"
                )
                continue
            session.nudged_at = now
            session.nudges += 1
            session.send(text=CUT_OFF_NOTE)
            journal.note(
                f"{session.issue.id} was cut off mid-response",
                f"Told to carry on (nudge {session.nudges} of {MAX_NUDGES}).",
            )
            nudged.append(session)
        return nudged

    def nudge_memory_kill(self, now: float | None = None) -> tuple[list[Session], list[Session]]:
        """Builders that stopped after a low-memory kill sit there, because Claude Code tells them not to retry on
        their own. After MEMORY_NUDGE_AFTER_SECONDS wake each one to retry the smallest failed step once, one
        container job at a time. A second kill in the same phase (or too many in all) is Elliot's call instead:
        one needs-you notification, no more nudges. Returns (woken, escalated)."""
        now = time.time() if now is None else now
        woken: list[Session] = []
        escalated: list[Session] = []
        for session in self.all():
            if session.stopped or session.finished or session.asleep or session.attention:
                continue
            if session.issue.id in self._nuked or session.issue.id in self.queued_ids():
                continue
            idle = now - session.pty.last_output
            if idle < MEMORY_NUDGE_AFTER_SECONDS:
                continue
            alive = session.pty.is_alive()
            if alive and WATCHER.running(session.pty.pid):
                continue
            phase = session.progress().phase
            if phase == "human":
                continue
            kill = memory_kill_of(session)
            if kill is None or kill["key"] == session.memory_kill_handled:
                continue
            session.memory_kill_handled = kill["key"]
            again = session.memory_nudges > 0 and session.memory_nudge_phase == phase
            if again or session.memory_nudges >= MAX_MEMORY_NUDGES:
                if not session.memory_escalated:
                    session.memory_escalated = True
                    notify(
                        f"{session.issue.id} needs you: killed for memory again",
                        f"{kill['step'][:120]} was killed again after one retry. Free memory, then wake it.",
                    )
                    escalated.append(session)
                self._persist()
                continue
            session.memory_nudges += 1
            session.memory_nudge_phase = phase
            step = "the builder" if kill["step"] == PROCESS_KILLED else f"`{kill['step']}`"
            note = MEMORY_KILL_NOTE.format(minutes=max(1, int(idle // 60)), step=step)
            try:
                self.wake(session.issue.id, note)
            except (locks.BuilderLockedError, UnmappedTeamError, WorktreeError, OSError) as failure:
                logger.warning(
                    "Could not wake %s after a memory kill: %s", session.issue.id, failure
                )
                self._persist()
                continue
            journal.note(
                f"{session.issue.id} stopped after a low-memory kill",
                f"Woken to retry the smallest failed step, one container job at a time ({kill['step'][:80]}).",
            )
            woken.append(session)
            self._persist()
        return woken, escalated

    def _log_skipped_cut_off(self, session: Session, reason: str) -> None:
        """Once per stall, so the log says why a builder showing an API error was left alone."""
        if session.cut_off_logged_at == session.pty.last_output:
            return
        session.cut_off_logged_at = session.pty.last_output
        logger.info("%s shows an API error but was not nudged: %s", session.issue.id, reason)

    def check_waiting(self, now: float | None = None) -> tuple[list[Session], list[Session]]:
        """Sessions that just started waiting on you, and ones due their single 5-minute reminder."""
        now = time.time() if now is None else now
        waiting = self._settings.waiting_after_seconds
        fresh: list[Session] = []
        reminders: list[Session] = []
        flipped = False
        for session in self.all():
            is_waiting = session.health(waiting) == "waiting"
            flipped = flipped or is_waiting != session.was_waiting
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
        if flipped:
            self._persist()
        return fresh, reminders


def notify(title: str, message: str) -> None:
    journal.note(title, message)
    if os.environ.get("CYCLE_NO_NOTIFY"):
        return
    safe = message.replace('"', "'")[:200]
    script = f'display notification "{safe}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not send a notification")
