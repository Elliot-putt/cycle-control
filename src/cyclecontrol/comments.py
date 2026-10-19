"""Review feedback on builder PRs: what is new, what the builder did about it, and what needs you."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKER = "<!-- spp-builder -->"
OTHER_BOTS = {
    "github-actions",
    "dependabot",
    "sonarcloud",
    "copilot-pull-request-reviewer",
    "codecov",
}
WALKTHROUGH_HINTS = ("auto-generated comment: summarize by coderabbit.ai", "walkthrough")
REVIEW_BODY_SECTIONS = ("nitpick comments", "outside diff range", "duplicate comments")
MAX_SEEN = 600
MAX_ITEMS = 200


DRAFT_PREFIX = re.compile(
    r"^\s*(?:draft(?:ed)?(?:\s+reply)?|proposed\s+reply)\s*(?:\(\s*not\s+posted\s*\))?\s*:\s*",
    re.IGNORECASE,
)


def clean_draft(text: str) -> str:
    """A drafted reply as it should read when posted: no "Draft reply (not posted):" label and no marker."""
    return DRAFT_PREFIX.sub("", text.replace(MARKER, ""), count=1).strip()


def is_coderabbit(login: str) -> bool:
    return "coderabbit" in login.lower()


def is_other_bot(login: str) -> bool:
    low = login.lower()
    return (low.endswith("[bot]") and not is_coderabbit(low)) or low in OTHER_BOTS


@dataclass(frozen=True)
class ReviewItem:
    key: str
    kind: str
    author: str
    is_owner: bool
    is_coderabbit: bool
    body: str
    path: str = ""
    line: int = 0
    url: str = ""
    thread_id: str = ""
    reply_to: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class Approval:
    approved: bool
    approvers: tuple[str, ...]
    blocking: tuple[str, ...]


def review_status(pr: dict[str, Any]) -> Approval:
    """Our team's convention: an approval with comments means address the feedback, then it is ready to merge.

    A reviewer approves when their latest review is APPROVED, or COMMENTED after they approved.
    Only a latest review of CHANGES_REQUESTED blocks. Bots and the PR author do not count.
    """
    author = (pr.get("author") or {}).get("login", "")
    latest: dict[str, str] = {}
    ever_approved: set[str] = set()
    for review in sorted(pr["reviews"]["nodes"], key=lambda r: r.get("submittedAt") or ""):
        login = _login(review)
        if not login or login == author or review["state"] == "PENDING":
            continue
        if (review.get("author") or {}).get("__typename") == "Bot":
            continue
        if review["state"] == "APPROVED":
            ever_approved.add(login)
        latest[login] = review["state"]
    approvers = tuple(
        login
        for login, state in latest.items()
        if state == "APPROVED" or (state == "COMMENTED" and login in ever_approved)
    )
    blocking = tuple(login for login, state in latest.items() if state == "CHANGES_REQUESTED")
    return Approval(bool(approvers) and not blocking, approvers, blocking)


def pr_state(pr: dict[str, Any]) -> dict[str, Any]:
    """What the lane needs to know about a PR: approval, CI, unresolved threads and labels."""
    approval = review_status(pr)
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = ((commits[-1]["commit"].get("statusCheckRollup") or {}) if commits else {}).get(
        "state"
    ) or ""
    return {
        "open": pr["state"] == "OPEN",
        "head": pr.get("headRefOid", ""),
        "draft": bool(pr.get("isDraft")),
        "approved": approval.approved,
        "approvers": list(approval.approvers),
        "blocking": list(approval.blocking),
        "ci": rollup,
        "unresolved": sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"]),
        "labels": [n["name"] for n in (pr.get("labels") or {}).get("nodes", [])],
    }


def is_requeue_candidate(state: dict[str, Any] | None, needs_you: int = 0) -> bool:
    """Blocked by the merge bot, but approved with every thread resolved and CI green: worth running requeue.sh."""
    if not state:
        return False
    return bool(
        state.get("open")
        and not state.get("draft")
        and state.get("approved")
        and state.get("unresolved", 1) == 0
        and state.get("ci") == "SUCCESS"
        and needs_you == 0
        and "blocked" in state.get("labels", [])
    )


def is_ready_to_merge(state: dict[str, Any] | None, needs_you: int = 0) -> bool:
    """Approved, every thread addressed, CI green, and not already queued or blocked."""
    if not state:
        return False
    labels = state.get("labels", [])
    return bool(
        state.get("open")
        and not state.get("draft")
        and state.get("approved")
        and state.get("unresolved", 1) == 0
        and state.get("ci") == "SUCCESS"
        and needs_you == 0
        and "mergequeue" not in labels
        and "blocked" not in labels
    )


@dataclass(frozen=True)
class PrFacts:
    number: int
    state: str
    author: str
    branch: str
    review_decision: str


def _login(node: dict[str, Any]) -> str:
    return (node.get("author") or {}).get("login", "")


def _skip_bot(node: dict[str, Any]) -> bool:
    """A bot other than CodeRabbit: GitHub's own Bot type, or a known name."""
    login = _login(node)
    if is_coderabbit(login):
        return False
    return (node.get("author") or {}).get("__typename") == "Bot" or is_other_bot(login)


def parse_pr(pr: dict[str, Any]) -> tuple[PrFacts, list[ReviewItem]]:
    """The actionable items of a PR: unresolved threads awaiting an answer, comments and review bodies."""
    author = (pr.get("author") or {}).get("login", "")
    facts = PrFacts(
        pr["number"], pr["state"], author, pr.get("headRefName", ""), pr.get("reviewDecision") or ""
    )
    items: list[ReviewItem] = []
    for thread in pr["reviewThreads"]["nodes"]:
        comments = thread["comments"]["nodes"]
        if thread["isResolved"] or not comments or MARKER in comments[-1]["body"]:
            continue
        last = comments[-1]
        login = _login(last)
        if _skip_bot(last):
            continue
        conversation = "\n".join(
            f"{'builder' if MARKER in c['body'] else '@' + _login(c)}: {c['body'].replace(MARKER, '').strip()}"
            for c in comments
        )
        items.append(
            ReviewItem(
                key=f"t:{last['databaseId']}",
                kind="thread",
                author=login,
                is_owner=login == author,
                is_coderabbit=is_coderabbit(login),
                body=conversation,
                path=thread.get("path") or "",
                line=thread.get("line") or 0,
                url=last.get("url", ""),
                thread_id=thread["id"],
                reply_to=comments[0]["databaseId"],
                created_at=last.get("createdAt", ""),
            )
        )
    for comment in pr["comments"]["nodes"]:
        login = _login(comment)
        body = comment["body"]
        if MARKER in body or _skip_bot(comment) or not body.strip():
            continue
        if is_coderabbit(login) and any(h in body[:800].lower() for h in WALKTHROUGH_HINTS):
            continue
        items.append(
            ReviewItem(
                f"c:{comment['databaseId']}",
                "comment",
                login,
                login == author,
                is_coderabbit(login),
                body,
                url=comment.get("url", ""),
                created_at=comment.get("createdAt", ""),
            )
        )
    for review in pr["reviews"]["nodes"]:
        login = _login(review)
        body = review.get("body") or ""
        if (
            MARKER in body
            or not body.strip()
            or review["state"] in ("APPROVED", "DISMISSED", "PENDING")
        ):
            continue
        if _skip_bot(review) or (
            is_coderabbit(login) and not any(s in body.lower() for s in REVIEW_BODY_SECTIONS)
        ):
            continue
        items.append(
            ReviewItem(
                f"r:{review['databaseId']}",
                "review",
                login,
                login == author,
                is_coderabbit(login),
                body,
                url=review.get("url", ""),
                created_at=review.get("submittedAt", ""),
            )
        )
    return facts, items


def approval_line(state: dict[str, Any] | None, items: list[ReviewItem]) -> str:
    if not state:
        return ""
    if state.get("approved"):
        who = ", ".join("@" + a for a in state.get("approvers", []))
        return f"Approval: approved ({who}). Address every thread, reply and resolve. Do not re-request review."
    blocking = state.get("blocking", [])
    asked = [
        *blocking,
        *[i.author for i in items if not i.is_owner and not i.is_coderabbit],
    ]
    names = ", ".join("@" + n for n in dict.fromkeys(asked))
    reason = (
        "changes requested by " + ", ".join("@" + b for b in blocking)
        if blocking
        else "no approval yet"
    )
    return f"Approval: not approved ({reason}). After fixing, re-request review from: {names or 'the reviewers who commented'}."


def plan_context(git_dir: Path, ticket_url: str = "") -> str:
    """What is already written down, so a builder can answer settled pushback itself."""
    try:
        plan = json.loads((git_dir / "spp-plan.json").read_text())
    except (OSError, ValueError):
        plan = {}
    plan = plan if isinstance(plan, dict) else {}
    lines = ["## Already documented (check before you escalate)"]
    if ticket_url:
        lines.append(f"- The ticket: {ticket_url}. Read its Decisions.")
    if plan.get("summary"):
        lines.append(f"- Plan: {str(plan['summary'])[:240]}")
    lines += [f"- Decision: {str(d)[:240]}" for d in plan.get("decisions", [])[:12]]
    lines += [f"- Risk noted in the plan: {str(r)[:240]}" for r in plan.get("risks", [])[:8]]
    lines.append("- The PR description, and anything Elliot told you during this run.")
    lines.append(
        "If one of these answers the comment, reply with it yourself and say where it is written."
    )
    return "\n".join(lines)


def feedback_text(
    repo: str,
    facts: PrFacts,
    items: list[ReviewItem],
    state: dict[str, Any] | None = None,
    context: str = "",
) -> str:
    lines = [
        f"# Review feedback on PR #{facts.number} ({repo})",
        f"Branch {facts.branch}. {len(items)} new item(s). Handle them under the Review comments rules in ~/.claude/agents/spp-builder.md.",
        'Record each with `~/.claude/spp/comment.sh <id> fixed|replied|skipped|needs_you "<reply or draft>"`.',
        approval_line(state, items),
        "",
        *([context, ""] if context else []),
    ]
    for n, item in enumerate(items, start=1):
        who = (
            "Elliot (an instruction)"
            if item.is_owner
            else ("CodeRabbit" if item.is_coderabbit else "reviewer")
        )
        where = f" {item.path}:{item.line}" if item.path else ""
        lines += [f"## {n}. id `{item.key}` · {item.kind} · @{item.author} · {who}{where}"]
        if item.thread_id:
            lines.append(f"Thread node id `{item.thread_id}`. Reply to comment `{item.reply_to}`.")
        lines += [item.url, "", *["> " + ln for ln in item.body.strip().splitlines()[:60]], ""]
    return "\n".join(lines)


class CommentStore:
    """What was seen, what waits for a builder, and what you decided, per PR. Survives restarts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        self._prs: dict[str, dict[str, Any]] = data.get("prs", {})
        self.numbers: dict[str, int] = data.get("numbers", {})
        self.flags: dict[str, Any] = data.get("flags", {})

    def pr(self, key: str) -> dict[str, Any]:
        return self._prs.setdefault(
            key, {"seen": [], "items": {}, "pending": [], "resolutions": {}, "baselined": False}
        )

    def peek(self, key: str) -> dict[str, Any] | None:
        return self._prs.get(key)

    def drop(self, key: str) -> None:
        self._prs.pop(key, None)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"prs": self._prs, "numbers": self.numbers, "flags": self.flags}, indent=1)
            + "\n"
        )

    def record(
        self, key: str, items: list[ReviewItem], now: float | None = None
    ) -> list[ReviewItem]:
        """Note what a poll found. Returns the items that are new and now wait for the builder."""
        state = self.pr(key)
        seen = set(state["seen"])
        first = not state["baselined"]
        fresh: list[ReviewItem] = []
        for item in items:
            known = item.key in seen
            state["items"].setdefault(
                item.key, {**asdict(item), "delivered_at": 0, "found_at": now or time.time()}
            )
            if known:
                continue
            seen.add(item.key)
            if first and item.kind != "thread":
                continue
            fresh.append(item)
        state["baselined"] = True
        state["seen"] = sorted(seen)[-MAX_SEEN:]
        state["pending"] = list(dict.fromkeys([*state["pending"], *[i.key for i in fresh]]))
        if len(state["items"]) > MAX_ITEMS:
            for stale in list(state["items"])[: len(state["items"]) - MAX_ITEMS]:
                state["items"].pop(stale)
        return fresh

    def pending_items(self, key: str) -> list[ReviewItem]:
        state = self.pr(key)
        return [
            ReviewItem(
                **{
                    k: v
                    for k, v in state["items"][k].items()
                    if k in ReviewItem.__dataclass_fields__
                }
            )
            for k in state["pending"]
            if k in state["items"]
        ]

    def set_state(self, key: str, state: dict[str, Any]) -> None:
        self.pr(key)["state"] = state

    def state_of(self, key: str) -> dict[str, Any] | None:
        return (self._prs.get(key) or {}).get("state")

    def mark_seen(self, key: str, item_keys: list[str]) -> None:
        """These items will never be delivered: they are seen, no longer pending, and count as delivered."""
        state = self.pr(key)
        state["seen"] = sorted(set(state["seen"]) | set(item_keys))[-MAX_SEEN:]
        state["pending"] = [k for k in state["pending"] if k not in item_keys]
        for item_key in item_keys:
            if item_key in state["items"] and not state["items"][item_key].get("delivered_at"):
                state["items"][item_key]["delivered_at"] = time.time()

    def notified(self, flag: str) -> Any:
        return self.flags.get(flag)

    def set_notified(self, flag: str, value: Any) -> None:
        self.flags[flag] = value

    def mark_delivered(self, key: str, now: float | None = None) -> None:
        state = self.pr(key)
        for item_key in state["pending"]:
            if item_key in state["items"]:
                state["items"][item_key]["delivered_at"] = now or time.time()
        state["pending"] = []

    def resolve(
        self, key: str, item_key: str, state_name: str, reply: str = "", **extra: Any
    ) -> None:
        self.pr(key)["resolutions"][item_key] = {
            "state": state_name,
            "reply": reply,
            "at": time.time(),
            **extra,
        }


def read_actions(git_dir: Path) -> dict[str, dict[str, str]]:
    """The last thing the builder recorded for each item, from spp-comments.jsonl."""
    actions: dict[str, dict[str, str]] = {}
    try:
        lines = (git_dir / "spp-comments.jsonl").read_text().splitlines()
    except OSError:
        return actions
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id"):
            actions[str(row["id"])] = {
                "action": str(row.get("action", "")),
                "reply": str(row.get("reply", "")),
                "at": str(row.get("at", "")),
            }
    return actions


def read_requeue(git_dir: Path) -> dict[str, Any] | None:
    """What requeue.sh last did: requeued, or refused with its reasons."""
    try:
        data = json.loads((git_dir / "spp-requeue.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lane_label(state: dict[str, Any] | None, needs_you: int, requeue: dict[str, Any] | None) -> str:
    """The one tag the lane shows for where the PR stands after review."""
    ready = is_ready_to_merge(state, needs_you)
    labels = (state or {}).get("labels", [])
    if (
        requeue
        and requeue.get("status") == "requeued"
        and "mergequeue" in labels
        and "blocked" not in labels
    ):
        return "re-queued"
    if requeue and requeue.get("status") == "refused" and not ready:
        return "re-queue refused"
    if ready:
        return "ready to merge"
    if "mergequeue" in labels and "blocked" not in labels:
        return "in merge queue"
    return ""


def review_stage(
    state: dict[str, Any] | None,
    needs_you: int,
    waiting: int,
    requeue: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What the PR is doing after review, in one headline and one next step."""
    if not state:
        return None
    labels = state.get("labels", [])
    unresolved = state.get("unresolved", 0)
    blocking = state.get("blocking", [])
    ci = str(state.get("ci") or "unknown").lower()
    if not state.get("open", True):
        return {"tone": "done", "headline": "Closed or merged", "next": "Nothing more to do."}
    if state.get("draft"):
        return {"tone": "wait", "headline": "Draft PR", "next": "Mark it ready for review."}
    if needs_you:
        return {
            "tone": "need",
            "headline": f"{needs_you} review comment{'s need' if needs_you != 1 else ' needs'} your decision",
            "next": "Open the Comments tab and answer or post the drafted reply.",
        }
    if waiting:
        return {
            "tone": "work",
            "headline": f"Builder is working through {waiting} review comment{'s' if waiting != 1 else ''}",
            "next": "It fixes, replies and resolves each thread. Nothing for you to do.",
        }
    if blocking:
        who = ", ".join("@" + b for b in blocking)
        return {
            "tone": "need",
            "headline": f"Changes requested by {who}",
            "next": "The builder picks the comments up on the next check.",
        }
    if unresolved:
        return {
            "tone": "wait",
            "headline": f"{unresolved} thread{'s' if unresolved != 1 else ''} still open",
            "next": "Waiting for the threads to be resolved.",
        }
    if ci != "success":
        return {
            "tone": "work" if ci in ("pending", "expected", "unknown") else "need",
            "headline": f"CI is {ci}",
            "next": "Waiting for the checks to finish.",
        }
    if not state.get("approved"):
        return {
            "tone": "wait",
            "headline": "Waiting for a reviewer's approval",
            "next": "Nothing for you or the builder until someone reviews.",
        }
    queued = "mergequeue" in labels
    if queued and "blocked" not in labels:
        return {
            "tone": "done",
            "headline": "In the merge queue",
            "next": "Aviator merges it when its turn comes. Nothing for you to do.",
        }
    if "blocked" in labels:
        if requeue and requeue.get("status") == "refused":
            why = "; ".join(requeue.get("reasons") or []) or "a check failed"
            return {
                "tone": "need",
                "headline": "Blocked by Aviator, re-queue refused",
                "next": f"Not re-queued: {why}.",
            }
        return {
            "tone": "work",
            "headline": "Blocked by Aviator, approved and clean",
            "next": "The app re-queues it within 5 minutes if you added mergequeue; otherwise add it yourself.",
        }
    return {
        "tone": "need",
        "headline": "Approved and ready to merge",
        "next": "Press Add to merge queue.",
    }


def comments_view(
    store_pr: dict[str, Any] | None,
    actions: dict[str, dict[str, str]],
    requeue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Counts, the lane tag and one row per item for the lane and the Comments tab."""
    if not store_pr:
        return {
            "addressed": 0,
            "needsYou": 0,
            "waiting": 0,
            "items": [],
            "lane": "",
            "authors": [],
            "status": None,
            "stage": None,
            "requeue": requeue,
        }
    rows: list[dict[str, Any]] = []
    for key, item in store_pr["items"].items():
        action = actions.get(key, {})
        decision = store_pr["resolutions"].get(key)
        if decision:
            state, reply = decision["state"], decision.get("reply", "")
        elif action:
            state, reply = action["action"], action.get("reply", "")
        elif item.get("delivered_at"):
            state, reply = "delivered", ""
        elif key in store_pr["pending"]:
            state, reply = "queued", ""
        else:
            state, reply = "seen", ""
        rows.append(
            {
                **{
                    k: item[k]
                    for k in ("key", "kind", "author", "path", "line", "body", "url")
                    if k in item
                },
                "state": state,
                "reply": clean_draft(reply),
                "postedUrl": (decision or {}).get("posted_url", ""),
                "resolved": bool((decision or {}).get("resolved")),
            }
        )
    relevant = [r for r in rows if r["state"] != "seen"]
    authors = list(
        dict.fromkeys(r["author"] for r in relevant if r["state"] in ("queued", "delivered"))
    )
    needs_you = sum(1 for r in relevant if r["state"] == "needs_you")
    state = store_pr.get("state")
    return {
        "authors": authors,
        "lane": lane_label(state, needs_you, requeue),
        "status": state,
        "stage": review_stage(
            state,
            needs_you,
            sum(1 for r in relevant if r["state"] in ("queued", "delivered")),
            requeue,
        ),
        "requeue": requeue,
        "addressed": sum(
            1 for r in relevant if r["state"] in ("fixed", "replied", "skipped", "posted")
        ),
        "needsYou": sum(1 for r in relevant if r["state"] == "needs_you"),
        "waiting": sum(1 for r in relevant if r["state"] in ("queued", "delivered")),
        "items": sorted(relevant, key=lambda r: (r["state"] != "needs_you", r["key"])),
    }


PR_URL = re.compile(r"/pull/(\d+)")
