"""Every few minutes, find new review feedback on builder PRs and hand it to the right builder.

Nothing here spends model tokens unless something is new. Feedback waits while a builder is busy,
and an ended builder is resumed, or restarted with the PR number if it cannot be resumed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from cyclecontrol.circleci import CircleCI
from cyclecontrol.comments import (
    BUILDER_KINDS,
    MARKER,
    CommentStore,
    PrFacts,
    ReviewItem,
    ci_item,
    clean_draft,
    comments_view,
    conflict_item,
    conflict_prompt,
    feedback_text,
    is_ready_to_merge,
    is_requeue_candidate,
    merge_label_for,
    mergeability_pending,
    merged_store_pr,
    parse_pr,
    plan_context,
    pr_state,
    queued,
    read_actions,
    read_requeue,
    resolved_thread_ids,
    threads_after_merge,
)
from cyclecontrol.config import Settings, TeamConfig
from cyclecontrol.github import (
    GithubError,
    comment_on_pr,
    fetch_pr_feedback,
    find_pr_any_state,
    find_pr_by_issue,
    reply_to_review_comment,
    resolve_thread,
)
from cyclecontrol.journal import note
from cyclecontrol.locks import BuilderLockedError
from cyclecontrol.sessions import PR_OPEN_PHASES, Session, SessionManager, notify

EARLY_PHASES = ("refine", "plan", "break", "breakdown", "build", "execute", "gates", "review")

logger = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 3
REQUEUE_RETRY_SECONDS = 300
QUEUE_POLL_SECONDS = 60
REQUEUE_SCRIPT = Path.home() / ".claude" / "spp" / "requeue.sh"
RESUME_GRACE_SECONDS = 45
RESUME_GIVE_UP_SECONDS = 600
REVIVE_RETRY_SECONDS = 60
CONFLICT_COOLDOWN_SECONDS = 600


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
        resolver: Callable[[str], None] = resolve_thread,
        find_by_issue: Callable[[str, str], tuple[str, int] | None] = find_pr_by_issue,
        circleci: CircleCI | None = None,
    ) -> None:
        self._circleci = circleci or CircleCI()
        self._settings = settings
        self._resolver = resolver
        self._sessions = sessions
        self._store = store
        self._fetch = fetch
        self._find_pr = find_pr
        self._find_by_issue = find_by_issue
        self.find_all_by_issue: Callable[[str, str], list[tuple[str, int]]] | None = None
        self._discovered: dict[str, float] = {}
        self._clock = clock
        self._notify = notifier
        self._run = runner
        self._warned: set[str] = set()
        self.last_poll: float | None = None
        self._resumed_at: dict[str, tuple[float, Session]] = {}
        self._revive_tried_at: dict[str, float] = {}

    @staticmethod
    def pr_key(team: TeamConfig, number: int) -> str:
        return f"{team.github_repo}#{number}"

    def _team_of(self, session: Session) -> TeamConfig | None:
        """The team, pointed at the repo the builder's PR is really in when that is a sibling repo."""
        team = self._settings.team_for(session.issue.team)
        repo = self._store.repos.get(session.issue.id)
        return replace(team, github_repo=repo) if team and repo else team

    def _locate(self, session: Session) -> tuple[TeamConfig, int] | None:
        team = self._team_of(session)
        if team is None:
            return None
        number = self._store.numbers.get(session.issue.id)
        rebuilding = session.pty.is_alive() and session.progress().phase in EARLY_PHASES
        if number is not None and (rebuilding or not session.finished):
            old = self._store.state_of(self.pr_key(team, number)) or {}
            if old.get("open") is False and not old.get("merged"):
                # The ticket was scrapped and rebuilt: its remembered PR was closed, so look for the new one.
                for table in (self._store.numbers, self._store.stacked, self._store.others):
                    table.pop(session.issue.id, None)
                self._store.repos.pop(session.issue.id, None)
                session.finished = False
                team = self._settings.team_for(session.issue.team) or team
                number = None
        if number is None:
            try:
                number = self._find_pr(team.github_repo, session.issue.branch).number
            except GithubError:
                found = self._find_elsewhere(team, session)
                if found is None:
                    return None
                team, number = found
            self._store.numbers[session.issue.id] = number
        return team, number

    def _stacked(self, session: Session, team: TeamConfig) -> list[int]:
        """The other PRs of a stack: every same-repo PR the builder printed besides the one it is tracked by."""
        primary = self._store.numbers.get(session.issue.id)
        known = list(self._store.stacked.get(session.issue.id, []))
        # Numbers stored from a wrapped link (36 for 36269) are prefixes of a real PR: drop them.
        every = [primary, *known]
        known = [
            n
            for n in known
            if not any(o is not None and o != n and str(o).startswith(str(n)) for o in every)
        ]
        for url in session.pr_urls():
            found = re.match(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", url)
            if found is None or found.group(1) != team.github_repo:
                continue
            number = int(found.group(2))
            if number != primary and number not in known:
                known.append(number)
        if known:
            self._store.stacked[session.issue.id] = known
        else:
            self._store.stacked.pop(session.issue.id, None)
        return known

    def forget_prs(self, issue_id: str) -> None:
        """A fresh start (the ticket was scrapped and rewritten): the PRs of the old attempt are not this builder's."""
        session = self._sessions.get(issue_id)
        if session is not None and session.pty.is_alive():
            return
        for table in (
            self._store.numbers,
            self._store.stacked,
            self._store.others,
            self._store.repos,
        ):
            table.pop(issue_id, None)
        self._discovered.pop(issue_id, None)
        self._store.save()

    def _discover_others(self, session: Session, team: TeamConfig) -> None:
        """PRs of the same ticket in sibling repos (street + consumer-v3 + infra), found by title, every few minutes."""
        if self.find_all_by_issue is None:
            return
        now = self._clock()
        if now - self._discovered.get(session.issue.id, 0) < 900 or session.finished:
            return
        # GitHub's search API allows about 30 calls a minute: spread the first lookups over several polls.
        if sum(1 for t in self._discovered.values() if now - t < 60) >= 4:
            return
        self._discovered[session.issue.id] = now
        try:
            found = self.find_all_by_issue(team.github_repo.split("/")[0], session.issue.id)
        except GithubError:
            return
        number = self._store.numbers.get(session.issue.id)
        own = {
            self.pr_key(team, n) for n in [number, *self._store.stacked.get(session.issue.id, [])]
        }
        others = [f"{r}#{n}" for r, n in found if f"{r}#{n}" not in own and r != team.github_repo]
        if others:
            self._store.others[session.issue.id] = sorted(set(others))
        else:
            self._store.others.pop(session.issue.id, None)

    def _other_prs(self, session: Session, team: TeamConfig) -> list[tuple[TeamConfig, int, str]]:
        out = []
        for key in self._store.others.get(session.issue.id, []):
            repo, _, n = key.rpartition("#")
            out.append((replace(team, github_repo=repo), int(n), key))
        return out

    def _known_prs(self, session: Session) -> list[tuple[TeamConfig, int, str]]:
        """Primary first, then the stacked PRs, without touching the network."""
        team = self._team_of(session)
        number = self._store.numbers.get(session.issue.id)
        if team is None or number is None:
            return []
        numbers = [number, *self._store.stacked.get(session.issue.id, [])]
        return [(team, n, self.pr_key(team, n)) for n in numbers] + self._other_prs(session, team)

    def _find_elsewhere(self, team: TeamConfig, session: Session) -> tuple[TeamConfig, int] | None:
        try:
            found = self._find_by_issue(team.github_repo.split("/")[0], session.issue.id)
        except GithubError:
            return None
        if found is None:
            return None
        repo, number = found
        self._store.repos[session.issue.id] = repo
        return replace(team, github_repo=repo), number

    def _due(self, key: str) -> bool:
        state = self._store.state_of(key) or {}
        seconds = (
            QUEUE_POLL_SECONDS
            if queued(state.get("labels", []))
            or "blocked" in state.get("labels", [])
            or mergeability_pending(state)
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
            numbers = [number, *self._stacked(session, team)]
            self._discover_others(session, team)
            polled = [self._poll_pr(session, team, n, only_due) for n in numbers]
            polled += [
                self._poll_pr(session, t, n, only_due) for t, n, _ in self._other_prs(session, team)
            ]
            if any(polled):
                woken.append(session.issue.id)
        self.last_poll = self._clock()
        self._store.save()
        return woken

    def _poll_pr(self, session: Session, team: TeamConfig, number: int, only_due: bool) -> bool:
        """Read one PR and record what is new on it. True when something fresh arrived."""
        key = self.pr_key(team, number)
        stored = self._store.state_of(key) or {}
        if stored.get("open") is False or (only_due and not self._due(key)):
            return False
        try:
            raw = self._fetch(team.github_repo, number)
        except GithubError:
            logger.warning("Could not read PR %s#%s", team.github_repo, number)
            return False
        facts, items = parse_pr(raw)
        owner = self._settings.github_login.lower()
        foreign = bool(owner and facts.author and facts.author.lower() != owner)
        if foreign and facts.state != "OPEN":
            # Someone else's PR that is merged or closed is still a fact about the ticket: record it so the
            # ticket can finish, but say nothing to the builder about it.
            self._store.set_state(key, pr_state(raw))
            self._store.pr(key).update({"checked_at": self._clock(), "last_new": 0})
            return False
        if foreign:
            self._warn_once(
                f"foreign:{key}",
                f"{session.issue.id}: PR {key} was opened by {facts.author}, not {owner}; leaving it alone",
            )
            return False
        if facts.state != "OPEN":
            self._store.set_state(key, pr_state(raw))
            self._store.pr(key).update({"checked_at": self._clock(), "last_new": 0})
            self._store.forget_flags(key)
            self._tell_about_late_threads(session, key, raw)
            return False
        state = pr_state(raw)
        self._resolve_answered_threads(key, raw, state)
        state = pr_state(raw)
        self._store.set_state(key, state)
        self._store.pr(key)["resolved_threads"] = resolved_thread_ids(raw)
        handed_over = session.progress().phase in PR_OPEN_PHASES
        repo_url = f"https://github.com/{team.github_repo}"
        conflict = conflict_item(state, repo_url, number)
        ci = None
        if handed_over and not conflict and state.get("failing"):
            reruns, rerun_note = self._rerun_infra(session, key, number, state)
            state = {**state, "ciReruns": reruns}
            self._store.set_state(key, state)
            ci = None if rerun_note is None else ci_item(state, repo_url, number, rerun_note)
        elif state.get("failing") is not None:
            previous = (
                int(stored.get("ciReruns") or 0) if stored.get("head") == state.get("head") else 0
            )
            state = {**state, "ciReruns": previous}
            self._store.set_state(key, state)
        self._store.discard_stale_ci(key, state.get("head", ""))
        items.extend([ci] if ci else [])
        fresh = self._store.record(key, items, self._clock())
        if conflict:
            self._dispatch_conflict(session, key, number, state, conflict)
        self._store.pr(key).update({"checked_at": self._clock(), "last_new": len(fresh)})
        return bool(fresh)

    def _rerun_infra(
        self, session: Session, key: str, number: int, state: dict[str, Any]
    ) -> tuple[int, str | None]:
        """A red check that died getting the code or the environment is re-run, up to ci_reruns times per head,
        before any builder sees it. Returns (re-runs so far, note for the builder or None while a re-run is pending)."""
        head = state.get("head", "")
        flag = f"ci-rerun:{key}:{head}"
        done = int(self._store.notified(flag) or 0)
        if not self._settings.auto_ci_rerun or not self._circleci.enabled:
            return done, ""
        read = f"ci-read:{key}:{head}:{done}"
        if self._store.notified(read) == "code":
            return done, ""
        links = state.get("failingLinks") or {}
        failures = [self._circleci.failure(key.split("#")[0], url) for url in links.values()]
        if not failures or any(f is None or not f.infra for f in failures):
            self._store.set_notified(read, "code")
            return done, ""
        worst = failures[0]
        if done >= self._settings.ci_reruns:
            return done, (
                f"CI already re-ran {done} time{'s' if done != 1 else ''} on this head and still fails in "
                f"'{worst.step}' with: {worst.line}\nTreat it as infrastructure: re-run it once more yourself "
                "(circleci rerun_workflow from the check link, from failed). If it fails again, record it "
                "needs_you with that line and the step name."
            )
        workflows = {f.workflow_id for f in failures if f}
        if not all(self._circleci.rerun_from_failed(wf) for wf in workflows):
            return done, ""
        self._store.set_notified(flag, done + 1)
        note(
            f"{session.issue.id} CI re-run {done + 1} of {self._settings.ci_reruns} on PR #{number}",
            f"{worst.job} failed in '{worst.step}': {worst.line}",
        )
        return done + 1, None

    def _tell_about_late_threads(self, session: Session, key: str, raw: dict[str, Any]) -> None:
        """Review threads opened after the merge reach nobody: say so once, with the links."""
        late = threads_after_merge(raw)
        flag = f"late-threads:{key}"
        if not late or self._store.notified(flag):
            return
        self._store.set_notified(flag, True)
        self._notify(
            f"{session.issue.id} got {len(late)} review thread{'s' if len(late) != 1 else ''} after merging",
            late[0],
        )

    def _dispatch_conflict(
        self, session: Session, key: str, number: int, state: dict[str, Any], item: ReviewItem
    ) -> bool:
        """A conflict goes straight to the builder, once per head sha. Nothing waits for you."""
        self._store.record(key, [item], self._clock())
        self._store.mark_seen(key, [item.key])
        if session.stopped:
            return False
        flag = f"conflict-sent:{key}:{state.get('head', '')}"
        if self._store.notified(flag):
            return False
        last = self._store.notified(f"conflict-last:{key}")
        if last and self._clock() - float(last) < CONFLICT_COOLDOWN_SECONDS:
            return False
        try:
            self._sessions.wake(session.issue.id, conflict_prompt(number, state.get("base") or ""))
        except Exception:
            logger.exception("Could not hand the conflict on %s to the builder", key)
            return False
        self._store.set_notified(flag, self._clock())
        self._store.set_notified(f"conflict-last:{key}", self._clock())
        if session.attention:
            self._sessions.clear_attention(session.issue.id)
        logger.info("Conflict on %s handed to %s", key, session.issue.id)
        note(
            f"{session.issue.id} has a merge conflict on PR #{number}",
            "Handed to the builder to resolve.",
        )
        return True

    def _answer_old_conflicts(self) -> None:
        """A conflict the builder raised as needs-you under the old rules is dispatched to it instead."""
        for session in list(self._sessions.all()):
            team = self._team_of(session)
            number = self._store.numbers.get(session.issue.id)
            if session.stopped or team is None or number is None:
                continue
            key = self.pr_key(team, number)
            store_pr = self._store.peek(key)
            if not store_pr:
                continue
            state = self._store.state_of(key) or {}
            head = state.get("head", "")
            flag = f"conflict-sent:{key}:{head}"
            actions = read_actions(session.git_dir)
            for item_key, item in list(store_pr["items"].items()):
                if item.get("kind") != "conflict" or store_pr["resolutions"].get(item_key):
                    continue
                if actions.get(item_key, {}).get("action") != "needs_you":
                    continue
                if self._store.notified(flag):
                    continue
                if state.get("conflicting") and item_key == f"conflict:{head[:8]}":
                    self._dispatch_conflict(
                        session,
                        key,
                        number,
                        state,
                        ReviewItem(
                            **{
                                k: v
                                for k, v in item.items()
                                if k in ReviewItem.__dataclass_fields__
                            }
                        ),
                    )
                    note = "handed to the builder to resolve itself"
                else:
                    note = "no longer conflicting"
                self._store.resolve(key, item_key, "answered", note)
                self._store.save()

    def _resolve_answered_threads(
        self, key: str, raw: dict[str, Any], state: dict[str, Any]
    ) -> None:
        """Safety net: a thread whose last word is the builder's reply is finished, so resolve it.

        An open thread blocks the merge queue. Skipped: a thread opened by someone who has requested
        changes (they should see the reply first) and one Elliot chose to answer without resolving.
        """
        if not self._settings.auto_resolve or not state.get("open"):
            return
        blocking = set(state.get("blocking", []))
        kept_open = {
            item.get("thread_id")
            for item_key, item in (self._store.peek(key) or {}).get("items", {}).items()
            if ((self._store.peek(key) or {}).get("resolutions", {}).get(item_key) or {}).get(
                "state"
            )
            == "posted"
            and not ((self._store.peek(key) or {}).get("resolutions", {}).get(item_key) or {}).get(
                "resolved"
            )
        }
        for thread in raw["reviewThreads"]["nodes"]:
            comments = thread["comments"]["nodes"]
            if thread["isResolved"] or not comments or MARKER not in comments[-1]["body"]:
                continue
            if (comments[0].get("author") or {}).get("login", "") in blocking or thread[
                "id"
            ] in kept_open:
                continue
            try:
                self._resolver(thread["id"])
            except GithubError:
                logger.warning("Could not resolve thread %s on %s", thread["id"], key)
                continue
            thread["isResolved"] = True

    def tick(self) -> list[str]:
        """The local step, cheap enough to run every couple of seconds: finish, resume, deliver, fall back."""
        self._after_resume()
        self._answer_old_conflicts()
        delivered: list[str] = []
        for session in list(self._sessions.all()):
            if session.stopped:
                continue
            known = self._known_prs(session)
            if not known:
                continue
            states = [self._store.state_of(k) for _, _, k in known]
            rebuilding = session.pty.is_alive() and session.progress().phase in EARLY_PHASES
            rebuilding = rebuilding and not any(s and s.get("merged") for s in states)
            if not rebuilding and all(s and not s.get("open", True) for s in states):
                if not session.finished:
                    note(
                        f"{session.issue.id} is done",
                        "Every PR is merged or closed; the builder is ended.",
                    )
                self._sessions.finish(session.issue.id)
                continue
            if session.finished:
                continue
            waiting = [(t, n, k) for t, n, k in known if self._store.pending_items(k)]
            if not waiting:
                continue
            team, number, key = waiting[0]
            if not session.pty.is_alive():
                self._revive(session, team, number)
            elif (
                session.health(self._settings.waiting_after_seconds) == "waiting"
                and not session.attention
            ):
                if self._rounds_exhausted(session, key, number):
                    continue
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
            team = self._team_of(session)
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
                label = merge_label_for(key.split("#")[0])
                labels = [name for name in state.get("labels", []) if name != "blocked"]
                labels += [label] if label else []
                self._store.set_state(key, {**state, "labels": labels})
                self._store.save()
                self._notify(f"{session.issue.id} was re-queued", session.issue.title)
            elif result.returncode != 0 and not note:
                logger.warning(
                    "requeue.sh failed for %s: %s", session.issue.id, str(result.stderr)[:200]
                )

    def _announce(self) -> None:
        """Tell you once when a PR becomes ready to merge, and when a re-queue was refused."""
        announced: set[str] = set()
        for session in self._sessions.all():
            for _team, number, key in self._known_prs(session):
                if key in announced:
                    # Two builders can share a PR (a stack's base); judging it twice with different
                    # answers flips its flag every tick and notifies every time.
                    continue
                announced.add(key)
                self._announce_pr(session, key, number)
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

    def _announce_pr(self, session: Session, key: str, number: int) -> None:
        view = comments_view(self._store.peek(key), read_actions(session.git_dir), None)
        ready = is_ready_to_merge(self._store.state_of(key), view["needsYou"])
        flag = f"ready:{key}"
        # The flag holds the head the PR was judged ready at, so a new commit is judged afresh and a verdict
        # on an outdated head never stands.
        head = (self._store.state_of(key) or {}).get("head") or True
        stamped = head if ready else False
        if ready and self._store.notified(flag) != stamped:
            self._notify(
                f"{session.issue.id} is ready to merge", f"PR #{number}: {session.issue.title}"
            )
        if ready:
            self._store.flags.pop(f"rounds:{key}", None)
            self._store.flags.pop(f"rounds-told:{key}", None)
        if self._store.notified(flag) != stamped:
            self._store.set_notified(flag, stamped)
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

    def _rounds_exhausted(self, session: Session, key: str, number: int) -> bool:
        """After MAX_REVIEW_ROUNDS hand-offs on one PR with feedback still pending, stop and bring in a human."""
        if int(self._store.notified(f"rounds:{key}") or 0) < MAX_REVIEW_ROUNDS:
            return False
        flag = f"rounds-told:{key}"
        if not self._store.notified(flag):
            self._notify(
                f"{session.issue.id} needs you: {MAX_REVIEW_ROUNDS} review rounds and still not clean",
                f"PR #{number} keeps coming back with feedback. Look at it, then answer or dismiss it.",
            )
            self._store.set_notified(flag, True)
            self._store.save()
        return True

    def _deliver(self, session: Session, team: TeamConfig, number: int, key: str) -> None:
        self._store.set_notified(
            f"rounds:{key}", int(self._store.notified(f"rounds:{key}") or 0) + 1
        )
        items = self._store.pending_items(key)
        facts = PrFacts(number, "OPEN", "", session.issue.branch, "")
        path = session.git_dir / "spp-feedback" / f"{int(self._clock())}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_pr_pointer(session, team, number)
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

    @staticmethod
    def _write_pr_pointer(session: Session, team: TeamConfig, number: int) -> None:
        """Which PR the builder's replies belong to, so comment.sh never guesses from the checked-out branch."""
        (session.git_dir / "spp-pr.json").write_text(
            json.dumps({"repo": team.github_repo, "number": number})
        )

    def _revive(self, session: Session, team: TeamConfig, number: int) -> None:
        issue_id = session.issue.id
        if issue_id in self._resumed_at:
            return
        if self._clock() - self._revive_tried_at.get(issue_id, -REVIVE_RETRY_SECONDS) < (
            REVIVE_RETRY_SECONDS
        ):
            return
        self._revive_tried_at[issue_id] = self._clock()
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
                    self._team_of(current),
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
        self._write_pr_pointer(session, team, number)
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
        team = self._team_of(session)
        number = self._store.numbers.get(session.issue.id)
        known = self._known_prs(session)
        records = [self._store.peek(k) for _, _, k in known]
        # The status shown is the first PR still open, so one merged PR of a stack never reads as the ticket merged.
        lead = next(
            (i for i, r in enumerate(records) if r and (r.get("state") or {}).get("open", True)), 0
        )
        store_pr = merged_store_pr(
            records[lead] if records else None,
            [r for i, r in enumerate(records) if i != lead],
        )
        stack = [
            {
                "number": n,
                "url": f"https://github.com/{t.github_repo}/pull/{n}",
                "merged": bool(r and (r.get("state") or {}).get("merged")),
                "open": bool(r is None or (r.get("state") or {}).get("open", True)),
            }
            for (t, n, _), r in zip(known, records)
        ]
        view = comments_view(store_pr, read_actions(session.git_dir), read_requeue(session.git_dir))
        delivered = [i.get("delivered_at", 0) for i in (store_pr or {}).get("items", {}).values()]
        view.update(
            {
                "stack": stack if len(stack) > 1 else [],
                "hasPr": number is not None,
                # Some repos (consumer-v3) have no merge queue: Approve there only posts to Slack.
                "noQueue": bool(
                    known
                    and not merge_label_for(known[0][0].github_repo, self._settings.merge_label)
                ),
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
        team = self._team_of(session)
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

    def answer_builder(self, session: Session, item_key: str, text: str) -> str:
        """A CI failure or conflict needs your call: the answer goes to the builder, never to GitHub.

        Wakes a sleeping or finished builder with the answer as its prompt. The item is marked
        answered and seen, so the same failing commit is not raised again.
        """
        team = self._team_of(session)
        number = self._store.numbers.get(session.issue.id)
        if team is None or number is None:
            raise GithubError("No PR is known for this builder.")
        key = self.pr_key(team, number)
        item = (self._store.peek(key) or {}).get("items", {}).get(item_key)
        if item is None:
            raise GithubError("Unknown item.")
        if item.get("kind") not in BUILDER_KINDS:
            raise GithubError("Only a CI or conflict item is answered to the builder.")
        text = text.strip()
        if not text:
            raise GithubError("The answer is empty.")
        outcome = self._sessions.wake(session.issue.id, text)
        self._store.mark_seen(key, [item_key])
        self._store.resolve(key, item_key, "answered", text)
        self._store.save()
        return outcome

    def dismiss(self, session: Session, item_key: str) -> None:
        team = self._team_of(session)
        number = self._store.numbers.get(session.issue.id)
        if team is None or number is None:
            return
        key = self.pr_key(team, number)
        self._store.flags.pop(f"rounds:{key}", None)
        self._store.flags.pop(f"rounds-told:{key}", None)
        self._store.mark_seen(key, [item_key])
        self._store.resolve(key, item_key, "dismissed")
        self._store.save()

    def seed_demo(self, session: Session) -> None:
        """Demo mode only: a few made-up items so the lane and the Comments tab have something to show."""
        team = self._team_of(session)
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
