"""Every few minutes, find new review feedback on builder PRs and hand it to the right builder.

Nothing here spends model tokens unless something is new. Feedback waits while a builder is busy,
and an ended builder is resumed, or restarted with the PR number if it cannot be resumed.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cyclecontrol.comments import (
    MARKER,
    CommentStore,
    PrFacts,
    ReviewItem,
    clean_draft,
    comments_view,
    feedback_text,
    is_ready_to_merge,
    is_requeue_candidate,
    parse_pr,
    plan_context,
    pr_state,
    read_actions,
    read_requeue,
)
from cyclecontrol.config import Settings, TeamConfig
from cyclecontrol.github import (
    GithubError,
    comment_on_pr,
    fetch_pr_feedback,
    find_pr_any_state,
    reply_to_review_comment,
    resolve_thread,
)
from cyclecontrol.locks import BuilderLockedError
from cyclecontrol.sessions import Session, SessionManager, notify

logger = logging.getLogger(__name__)

REQUEUE_RETRY_SECONDS = 300
QUEUE_POLL_SECONDS = 60
REQUEUE_SCRIPT = Path.home() / ".claude" / "spp" / "requeue.sh"
RESUME_GRACE_SECONDS = 45
RESUME_GIVE_UP_SECONDS = 600


def find_latest_pr(repo: str, branch: str) -> Any:
    found = find_pr_any_state(repo, branch)
    if found is None:
        raise GithubError(f"No PR for {branch}")
    return found


class CommentWatcher:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionManager,
        store: CommentStore,
        fetch: Callable[[str, int], dict[str, Any]] = fetch_pr_feedback,
        find_pr: Callable[[str, str], Any] = find_latest_pr,
        clock: Callable[[], float] = time.time,
        notifier: Callable[[str, str], None] = notify,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._store = store
        self._fetch = fetch
        self._find_pr = find_pr
        self._clock = clock
        self._notify = notifier
        self._run = runner
        self._warned: set[str] = set()
        self.last_poll: float | None = None
        self._resumed_at: dict[str, tuple[float, Session]] = {}

    @staticmethod
    def pr_key(team: TeamConfig, number: int) -> str:
        return f"{team.github_repo}#{number}"

    def _locate(self, session: Session) -> tuple[TeamConfig, int] | None:
        team = self._settings.team_for(session.issue.team)
        if team is None:
            return None
        number = self._store.numbers.get(session.issue.id)
        if number is None:
            try:
                number = self._find_pr(team.github_repo, session.issue.branch).number
            except GithubError:
                return None
            self._store.numbers[session.issue.id] = number
        return team, number

    def _due(self, key: str) -> bool:
        state = self._store.state_of(key) or {}
        seconds = (
            QUEUE_POLL_SECONDS
            if "mergequeue" in state.get("labels", [])
            else self._settings.comment_poll_seconds
        )
        checked = (self._store.peek(key) or {}).get("checked_at")
        return checked is None or self._clock() - checked >= seconds

    def poll(self, only_due: bool = False) -> list[str]:
        """The network step: look at every open builder PR. Returns the builders with something new."""
        woken: list[str] = []
        for session in list(self._sessions.all()):
            if session.stopped:
                continue
            located = self._locate(session)
            if located is None:
                continue
            team, number = located
            key = self.pr_key(team, number)
            stored = self._store.state_of(key) or {}
            if stored.get("open") is False or (only_due and not self._due(key)):
                continue
            try:
                raw = self._fetch(team.github_repo, number)
            except GithubError:
                logger.warning("Could not read PR %s#%s", team.github_repo, number)
                continue
            facts, items = parse_pr(raw)
            if facts.state != "OPEN":
                self._store.set_state(key, pr_state(raw))
                self._store.pr(key).update({"checked_at": self._clock(), "last_new": 0})
                continue
            self._store.set_state(key, pr_state(raw))
            fresh = self._store.record(key, items, self._clock())
            self._store.pr(key).update({"checked_at": self._clock(), "last_new": len(fresh)})
            if fresh:
                woken.append(session.issue.id)
        self.last_poll = self._clock()
        self._store.save()
        return woken

    def tick(self) -> list[str]:
        """The local step, cheap enough to run every couple of seconds: resume, deliver, fall back."""
        self._after_resume()
        delivered: list[str] = []
        for session in list(self._sessions.all()):
            if session.stopped:
                continue
            number = self._store.numbers.get(session.issue.id)
            team = self._settings.team_for(session.issue.team)
            if number is None or team is None:
                continue
            key = self.pr_key(team, number)
            if not self._store.pending_items(key):
                continue
            if not session.pty.is_alive():
                self._revive(session, team, number)
            elif (
                session.health(self._settings.waiting_after_seconds) == "waiting"
                and not session.attention
            ):
                self._deliver(session, team, number, key)
                delivered.append(session.issue.id)
        self._try_requeue()
        self._announce()
        return delivered

    def _try_requeue(self) -> None:
        """Run requeue.sh when the PR is blocked but otherwise ready. The script checks every condition itself."""
        if not self._settings.auto_requeue:
            return
        for session in self._sessions.all():
            team = self._settings.team_for(session.issue.team)
            number = self._store.numbers.get(session.issue.id)
            if session.stopped or team is None or number is None:
                continue
            key = self.pr_key(team, number)
            state = self._store.state_of(key)
            needs = comments_view(self._store.peek(key), read_actions(session.git_dir), None)[
                "needsYou"
            ]
            if not is_requeue_candidate(state, needs):
                continue
            if (
                session.pty.is_alive()
                and session.health(self._settings.waiting_after_seconds) != "waiting"
            ):
                continue
            attempt = int(time.time() // REQUEUE_RETRY_SECONDS)
            flag = f"requeue-try:{key}:{state.get('head', '')}:{attempt}"
            if self._store.notified(flag):
                continue
            self._store.set_notified(flag, True)
            self._store.save()
            result = self._run(
                ["bash", str(REQUEUE_SCRIPT), str(number)],
                cwd=str(session.worktree),
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            note = read_requeue(session.git_dir)
            if result.returncode == 0 and note and note.get("status") == "requeued":
                labels = [name for name in state.get("labels", []) if name != "blocked"] + [
                    "mergequeue"
                ]
                self._store.set_state(key, {**state, "labels": labels})
                self._store.save()
                self._notify(f"{session.issue.id} was re-queued", session.issue.title)
            elif result.returncode != 0 and not note:
                logger.warning(
                    "requeue.sh failed for %s: %s", session.issue.id, str(result.stderr)[:200]
                )

    def _announce(self) -> None:
        """Tell you once when a PR becomes ready to merge, and when a re-queue was refused."""
        for session in self._sessions.all():
            team = self._settings.team_for(session.issue.team)
            number = self._store.numbers.get(session.issue.id)
            if team is None or number is None:
                continue
            key = self.pr_key(team, number)
            view = comments_view(self._store.peek(key), read_actions(session.git_dir), None)
            ready = is_ready_to_merge(self._store.state_of(key), view["needsYou"])
            flag = f"ready:{key}"
            if ready and not self._store.notified(flag):
                self._notify(f"{session.issue.id} is ready to merge", session.issue.title)
            if ready != bool(self._store.notified(flag)):
                self._store.set_notified(flag, ready)
                self._store.save()
            for row in view["items"]:
                flag = f"needs:{key}:{row['key']}"
                if row["state"] == "needs_you" and not self._store.notified(flag):
                    first = str(row["body"]).strip().splitlines()[0][:120] if row["body"] else ""
                    self._notify(
                        f"{session.issue.id}: a review comment needs you",
                        f"@{row['author']}: {first}",
                    )
                    self._store.set_notified(flag, True)
                    self._store.save()
            requeue = read_requeue(session.git_dir)
            flag = f"requeue:{session.issue.id}"
            if (
                requeue
                and requeue.get("status") == "refused"
                and self._store.notified(flag) != requeue.get("at")
            ):
                reasons = "; ".join(requeue.get("reasons", []))
                self._notify(f"{session.issue.id} was not re-queued", reasons or "see the lane")
                self._store.set_notified(flag, requeue.get("at"))
                self._store.save()

    def _deliver(self, session: Session, team: TeamConfig, number: int, key: str) -> None:
        items = self._store.pending_items(key)
        facts = PrFacts(number, "OPEN", "", session.issue.branch, "")
        path = session.git_dir / "spp-feedback" / f"{int(self._clock())}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        context = plan_context(session.git_dir, session.issue.url)
        path.write_text(
            feedback_text(team.github_repo, facts, items, self._store.state_of(key), context)
        )
        session.send(
            text=(
                f"New review feedback on PR #{number}: read {path} and handle it under the Review comments "
                "rules in ~/.claude/agents/spp-builder.md. Record each item with ~/.claude/spp/comment.sh."
            )
        )
        self._store.mark_delivered(key, self._clock())
        self._store.save()
        who = ", ".join(dict.fromkeys("@" + i.author for i in items))
        self._notify(
            f"{session.issue.id} is picking up {len(items)} review comment{'s' if len(items) != 1 else ''}",
            f"From {who}",
        )

    def _revive(self, session: Session, team: TeamConfig, number: int) -> None:
        issue_id = session.issue.id
        if issue_id in self._resumed_at:
            return
        free, why = self._sessions.worktree_free(session)
        if not free:
            self._warn_once(issue_id, f"{issue_id}: not resuming yet, {why}")
            return
        self._resumed_at[issue_id] = (self._clock(), session)
        try:
            self._sessions.revive_for_comments(
                session.issue, session.mode, session.base, session.issue.branch
            )
        except BuilderLockedError as failure:
            del self._resumed_at[issue_id]
            self._warn_once(issue_id, f"{issue_id}: {failure}")
        except Exception:
            logger.exception("Could not resume %s for review feedback", issue_id)
            self._fresh(session, team, number)

    def _warn_once(self, issue_id: str, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            logger.warning(message)

    def _after_resume(self) -> None:
        for issue_id, (started, old) in list(self._resumed_at.items()):
            current = self._sessions.get(issue_id)
            age = self._clock() - started
            if current is None or current is old:
                if age > RESUME_GIVE_UP_SECONDS:
                    del self._resumed_at[issue_id]
            elif current.pty.is_alive():
                if age > RESUME_GRACE_SECONDS:
                    del self._resumed_at[issue_id]
            else:
                free, why = self._sessions.worktree_free(current)
                if not free:
                    self._warn_once(issue_id, f"{issue_id}: not starting a new builder, {why}")
                    if age > RESUME_GIVE_UP_SECONDS:
                        del self._resumed_at[issue_id]
                    continue
                del self._resumed_at[issue_id]
                self._fresh(
                    current,
                    self._settings.team_for(current.issue.team),
                    self._store.numbers.get(issue_id, 0),
                )

    def _fresh(self, session: Session, team: TeamConfig | None, number: int) -> None:
        """The old conversation could not be resumed: a new spp-builder in the same worktree, told the PR."""
        if team is None or not number:
            return
        free, why = self._sessions.worktree_free(session)
        if not free:
            self._warn_once(
                session.issue.id, f"{session.issue.id}: not starting a new builder, {why}"
            )
            return
        key = self.pr_key(team, number)
        items = self._store.pending_items(key)
        path = session.git_dir / "spp-feedback" / f"{int(self._clock())}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            feedback_text(
                team.github_repo,
                PrFacts(number, "OPEN", "", session.issue.branch, ""),
                items,
                self._store.state_of(key),
                plan_context(session.git_dir, session.issue.url),
            )
        )
        prompt = (
            f"You are the builder for PR #{number} of {session.issue.id} on branch {session.issue.branch}. "
            f"Review feedback is waiting: read {path} and handle it under the Review comments rules in "
            "~/.claude/agents/spp-builder.md. Record each item with ~/.claude/spp/comment.sh."
        )
        try:
            self._sessions.start_fresh_for_pr(session.issue, session.mode, session.base, prompt)
        except Exception:
            logger.exception("Could not start a new builder for %s", session.issue.id)
            return
        self._store.mark_delivered(key, self._clock())
        self._store.save()

    def view(self, session: Session) -> dict[str, Any]:
        team = self._settings.team_for(session.issue.team)
        number = self._store.numbers.get(session.issue.id)
        store_pr = self._store.peek(self.pr_key(team, number)) if team and number else None
        view = comments_view(store_pr, read_actions(session.git_dir), read_requeue(session.git_dir))
        delivered = [i.get("delivered_at", 0) for i in (store_pr or {}).get("items", {}).values()]
        view.update(
            {
                "hasPr": number is not None,
                "prNumber": number or 0,
                "checkedAt": (store_pr or {}).get("checked_at", 0),
                "lastNew": (store_pr or {}).get("last_new", 0),
                "deliveredAt": max(delivered, default=0),
                "intervalSeconds": self._settings.comment_poll_seconds,
                "enabled": self._settings.auto_comments,
                "repoUrl": f"https://github.com/{team.github_repo}" if team else "",
            }
        )
        return view

    def post_reply(self, session: Session, item_key: str, body: str, resolve: bool = False) -> None:
        """You approved or edited a drafted reply: post it, marked as the builder's so it is not re-delivered."""
        team = self._settings.team_for(session.issue.team)
        number = self._store.numbers.get(session.issue.id)
        if team is None or number is None:
            raise GithubError("No PR is known for this builder.")
        key = self.pr_key(team, number)
        item = (self._store.peek(key) or {}).get("items", {}).get(item_key)
        if item is None:
            raise GithubError("Unknown comment.")
        body = clean_draft(body)
        if not body:
            raise GithubError("The reply is empty.")
        original = clean_draft(read_actions(session.git_dir).get(item_key, {}).get("reply", ""))
        unchanged = bool(original) and body == original
        text = f"{body}\n\n{MARKER}"
        repo_url = f"https://github.com/{team.github_repo}"
        if item["kind"] == "thread" and item.get("reply_to"):
            posted = reply_to_review_comment(team.github_repo, number, int(item["reply_to"]), text)
            posted_key, posted_url = f"t:{posted}", f"{repo_url}/pull/{number}#discussion_r{posted}"
        else:
            posted = comment_on_pr(team.github_repo, number, f"> @{item['author']}\n\n{text}")
            posted_key, posted_url = (
                f"c:{posted}",
                f"{repo_url}/pull/{number}#issuecomment-{posted}",
            )
        self._store.mark_seen(key, [item_key, posted_key] if posted else [item_key])
        self._store.resolve(
            key,
            item_key,
            "posted",
            body,
            posted_url=posted_url if posted else "",
            resolved=False,
            unchanged=unchanged,
        )
        self._store.save()
        if resolve and item.get("thread_id"):
            try:
                resolve_thread(item["thread_id"])
            except GithubError as failure:
                raise GithubError(
                    f"The reply is posted, but the thread could not be resolved: {failure}"
                ) from failure
            self._store.resolve(
                key,
                item_key,
                "posted",
                body,
                posted_url=posted_url if posted else "",
                resolved=True,
                unchanged=unchanged,
            )
            self._store.save()

    def dismiss(self, session: Session, item_key: str) -> None:
        team = self._settings.team_for(session.issue.team)
        number = self._store.numbers.get(session.issue.id)
        if team is None or number is None:
            return
        key = self.pr_key(team, number)
        self._store.mark_seen(key, [item_key])
        self._store.resolve(key, item_key, "dismissed")
        self._store.save()

    def seed_demo(self, session: Session) -> None:
        """Demo mode only: a few made-up items so the lane and the Comments tab have something to show."""
        team = self._settings.team_for(session.issue.team)
        if team is None:
            return
        key = self.pr_key(team, 1)
        self._store.numbers[session.issue.id] = 1
        items = [
            ReviewItem(
                "t:1",
                "thread",
                "sam",
                False,
                False,
                "@sam: This name is unclear, call it reminderSchedule.",
                "app/Reminder.php",
                12,
                "https://example.com/1",
                "T1",
                1,
            ),
            ReviewItem(
                "t:2",
                "thread",
                "coderabbitai[bot]",
                False,
                True,
                "@coderabbitai[bot]: Nit: add a blank line.",
                "app/Reminder.php",
                40,
                "https://example.com/2",
                "T2",
                2,
            ),
            ReviewItem(
                "c:3",
                "comment",
                "sam",
                False,
                False,
                "Is the 5 minute window in the spec?",
                url="https://example.com/3",
            ),
            ReviewItem(
                "t:4",
                "thread",
                "sam",
                False,
                False,
                "@sam: Please drop the retry. It is not needed.",
                "app/Send.php",
                8,
                "https://example.com/4",
                "T4",
                4,
            ),
            ReviewItem(
                "t:5",
                "thread",
                "lee",
                False,
                False,
                "@lee: Add a test for the empty case.",
                "app/Send.php",
                20,
                "https://example.com/5",
                "T5",
                5,
            ),
        ]
        self._store.record(key, items, self._clock())
        self._store.mark_delivered(key, self._clock())
        self._store.pr(key).update({"checked_at": self._clock(), "last_new": len(items)})
        rows = [
            ("t:1", "fixed", "Renamed to reminderSchedule (fdce28a5)."),
            ("t:2", "skipped", ""),
            ("c:3", "replied", "Yes, it is in section 2 of the spec."),
            (
                "t:4",
                "needs_you",
                "Draft reply (not posted): The retry covers a flaky provider, so I would keep it. Happy to drop it if you prefer. <!-- spp-builder -->",
            ),
        ]
        import json

        lines = [
            json.dumps({"id": i, "action": a, "reply": r, "at": "2026-10-19T12:00:00Z"})
            for i, a, r in rows
        ]
        Path(session.git_dir / "spp-comments.jsonl").write_text("\n".join(lines) + "\n")
