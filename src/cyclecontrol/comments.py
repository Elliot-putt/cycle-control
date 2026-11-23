"""Review feedback on builder PRs: what is new, what the builder did about it, and what needs you."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
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
PRAISE_ONLY = re.compile(
    r"^(lgtm|looks good( to me)?|approved?|ship it|nice( one)?|great|thanks?( you)?|:\+1:|👍|✅)[\s.!]*$",
    re.IGNORECASE,
)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
MAX_SEEN = 600
MAX_ITEMS = 200
FIRST_LOOK_FRESH_SECONDS = 24 * 3600
INLINE_ONLY = re.compile(
    r"^(?=.{0,240}$)(?!.*\b(?:please|could|should|would|worth|before merge|must|needs?)\b).*\b(?:inline|below|in the threads?)\b",
    re.IGNORECASE | re.DOTALL,
)


DRAFT_PREFIX = re.compile(
    r"^\s*(?:draft(?:ed)?(?:\s+reply)?|proposed\s+reply)\s*(?:\(\s*not\s+posted\s*\))?\s*:\s*",
    re.IGNORECASE,
)


def clean_draft(text: str) -> str:
    """A drafted reply as it should read when posted: no "Draft reply (not posted):" label and no marker."""
    return DRAFT_PREFIX.sub("", text.replace(MARKER, ""), count=1).strip()


def points_inline(body: str) -> bool:
    """An approval body that only says its comments are inline asks for nothing itself."""
    text = visible(body)
    return bool(PRAISE_ONLY.match(text) or INLINE_ONLY.match(text))


def visible(text: str) -> str:
    """The body without hidden HTML comments such as CodeRabbit's generated-by marker."""
    return HTML_COMMENT.sub("", text).strip()


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


NOT_CI = ("aviator", "coderabbit", "auto-merge", "codecov")
REPO_MERGE_LABELS = {
    "AgentSoftware/street": "Merge Queue: Good to Go",
    "AgentSoftware/street-infra": "",
}
MERGE_LABELS = ("mergequeue", *(label for label in REPO_MERGE_LABELS.values() if label))


def merge_label_for(repo: str, default: str = "mergequeue") -> str:
    """The label Aviator watches in this repo, or "" where there is no merge queue."""
    return REPO_MERGE_LABELS.get(repo, default)


def queued(labels: list[str]) -> bool:
    """Carries the label Aviator watches, whichever name this repo uses."""
    return any(label in MERGE_LABELS for label in labels)


FAILED = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")


def latest_checks(rollup: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One node per check name. A re-run GitHub Action leaves its cancelled older runs on the commit;
    only the newest run of a name counts."""
    nodes = ((rollup or {}).get("contexts") or {}).get("nodes") or []
    newest: dict[str, dict[str, Any]] = {}
    for node in nodes:
        name = node.get("name") or node.get("context") or ""
        if any(skip in name.lower() for skip in NOT_CI):
            continue
        current = newest.get(name)
        if current is None or str(node.get("startedAt") or "") >= str(
            current.get("startedAt") or ""
        ):
            newest[name] = node
    return list(newest.values())


def check_verdict(node: dict[str, Any]) -> str:
    if node.get("__typename") == "CheckRun":
        result = node.get("conclusion") if node.get("status") == "COMPLETED" else "PENDING"
    else:
        result = node.get("state")
    return result or "PENDING"


def real_checks(rollup: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(name, verdict) of the checks that are really CI, in the order GitHub lists them."""
    return [
        (n.get("name") or n.get("context") or "", check_verdict(n)) for n in latest_checks(rollup)
    ]


def check_links(rollup: dict[str, Any] | None) -> dict[str, str]:
    """name -> the page behind each real check, for reading a failure or re-running it."""
    return {
        (n.get("name") or n.get("context") or ""): str(
            n.get("targetUrl") or n.get("detailsUrl") or ""
        )
        for n in latest_checks(rollup)
    }


def effective_ci(rollup: dict[str, Any] | None) -> str:
    """CI from the checks that are really CI. The merge bot and CodeRabbit sit pending on their own schedule."""
    if not rollup:
        return ""
    if not ((rollup.get("contexts") or {}).get("nodes") or []):
        return rollup.get("state") or ""
    verdicts = [verdict for _, verdict in real_checks(rollup)]
    if any(v in FAILED for v in verdicts):
        return "FAILURE"
    if any(v not in PASSED for v in verdicts):
        return "PENDING"
    return "SUCCESS" if verdicts else ""


def aviator_conflict(pr: dict[str, Any]) -> bool:
    """Aviator swapped mergequeue for blocked and said why: a conflict GitHub's mergeable may not show yet."""
    labels = [n["name"] for n in (pr.get("labels") or {}).get("nodes", [])]
    if "blocked" not in labels:
        return False
    return any(
        "aviator" in _login(node).lower() and "conflict" in str(node.get("body", "")).lower()
        for node in (pr.get("comments") or {}).get("nodes", [])
    )


def conflict_prompt(number: int, base: str) -> str:
    """What the builder is told, with no human in the loop, when its PR conflicts with the base."""
    return (
        f"PR #{number} has merge conflicts with {base}. Resolve them yourself now: rebase onto origin/{base} (never merge it in), "
        "keep the base's version and re-apply this PR's intent, run the touched tests, push with --force-with-lease, then run "
        "~/.claude/spp/requeue.sh so Aviator re-queues it. Do not ask Elliot."
    )


def threads_after_merge(pr: dict[str, Any]) -> list[str]:
    """Unresolved human threads opened on or after the merge: nobody is polling for them any more."""
    merged_at = pr.get("mergedAt") or ""
    if pr.get("state") != "MERGED" or not merged_at:
        return []
    found: list[str] = []
    for thread in pr["reviewThreads"]["nodes"]:
        comments = thread["comments"]["nodes"]
        if thread["isResolved"] or not comments or _skip_bot(comments[0]):
            continue
        if is_coderabbit(_login(comments[0])) or (comments[0].get("createdAt") or "") < merged_at:
            continue
        found.append(comments[0].get("url") or f"t:{comments[0]['databaseId']}")
    return found


def pr_state(pr: dict[str, Any]) -> dict[str, Any]:
    """What the lane needs to know about a PR: approval, CI, unresolved threads and labels."""
    approval = review_status(pr)
    commits = (pr.get("commits") or {}).get("nodes") or []
    checks = commits[-1]["commit"].get("statusCheckRollup") if commits else None
    rollup = effective_ci(checks)
    return {
        "open": pr["state"] == "OPEN",
        "merged": pr["state"] == "MERGED",
        "mergedAt": pr.get("mergedAt") or "",
        "head": pr.get("headRefOid", ""),
        "draft": bool(pr.get("isDraft")),
        "conflicting": pr.get("mergeable") == "CONFLICTING" or aviator_conflict(pr),
        "mergeable": pr.get("mergeable") or "",
        "base": pr.get("baseRefName", ""),
        "approved": approval.approved,
        "approvers": list(approval.approvers),
        "blocking": list(approval.blocking),
        "ci": rollup,
        "failing": [n for n, v in real_checks(checks) if v in FAILED],
        "failingLinks": {
            n: u
            for n, u in check_links(checks).items()
            if n in {f for f, v in real_checks(checks) if v in FAILED}
        },
        "unresolved": sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"]),
        "labels": [n["name"] for n in (pr.get("labels") or {}).get("nodes", [])],
    }


def mergeability_pending(state: dict[str, Any]) -> bool:
    """GitHub recomputes mergeability after a push or a base move and answers UNKNOWN meanwhile."""
    return state.get("mergeable") == "UNKNOWN"


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
        and not state.get("conflicting")
        and not mergeability_pending(state)
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
        and not queued(labels)
        and "blocked" not in labels
        and not state.get("conflicting")
        and not mergeability_pending(state)
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


def resolved_thread_ids(pr: dict[str, Any]) -> list[str]:
    return [t["id"] for t in pr["reviewThreads"]["nodes"] if t.get("isResolved") and t.get("id")]


def ci_item(
    state: dict[str, Any], repo_url: str, number: int, reruns: str = ""
) -> ReviewItem | None:
    """A failed CI run on a PR whose builder has already handed over."""
    failing = state.get("failing") or []
    head = state.get("head", "")
    if not failing or not head or not state.get("open") or state.get("draft"):
        return None
    body = (
        f"CI is failing on {head[:8]}: {', '.join(failing)}.\n"
        + (f"{reruns}\n" if reruns else "")
        + "Aviator merges develop into the branch after mergequeue, so this can be new develop code that clashes with this PR.\n"
        "Check the current state first: if CI is already green, record it as skipped. "
        "Otherwise read each failing job's log yourself (the circleci MCP get_build_failure_logs, or gh pr checks), fix it on your branch, "
        "run the fast gates, push with --force-with-lease, then run ~/.claude/spp/requeue.sh if the PR is blocked.\n"
        f'Record it with `~/.claude/spp/comment.sh ci:{head[:8]} fixed "<what broke and the fix>"`. It posts nothing to GitHub.'
    )
    return ReviewItem(
        key=f"ci:{head[:8]}",
        kind="ci",
        author="CI",
        is_owner=False,
        is_coderabbit=False,
        body=body,
        url=f"{repo_url}/pull/{number}",
        created_at="",
    )


def conflict_item(state: dict[str, Any], repo_url: str, number: int) -> ReviewItem | None:
    """A merge conflict on a PR whose builder has already handed over."""
    head = state.get("head", "")
    if not state.get("conflicting") or not head or not state.get("open") or state.get("draft"):
        return None
    base = state.get("base") or "the base branch"
    body = (
        f"{conflict_prompt(number, base)} (head {head[:8]})\n"
        f'Record it with `~/.claude/spp/comment.sh conflict:{head[:8]} fixed "<what you did>"`. It posts nothing to GitHub.'
    )
    return ReviewItem(
        key=f"conflict:{head[:8]}",
        kind="conflict",
        author="GitHub",
        is_owner=False,
        is_coderabbit=False,
        body=body,
        url=f"{repo_url}/pull/{number}",
        created_at="",
    )


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
            f"{'builder' if MARKER in c['body'] else '@' + _login(c)}: {visible(c['body'].replace(MARKER, ''))}"
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
        if MARKER in body or _skip_bot(comment) or not body.strip() or is_coderabbit(login):
            continue
        items.append(
            ReviewItem(
                f"c:{comment['databaseId']}",
                "comment",
                login,
                login == author,
                is_coderabbit(login),
                visible(body),
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
            or review["state"] in ("DISMISSED", "PENDING")
            or (review["state"] == "APPROVED" and points_inline(body))
            or _skip_bot(review)
            or is_coderabbit(login)
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
        *[i.author for i in items if i.kind not in BUILDER_KINDS and not i.is_owner],
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
        if item.kind in ("ci", "conflict"):
            lines += [f"## {n}. id `{item.key}` · {item.kind}", *item.body.strip().splitlines(), ""]
            continue
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
        self.repos: dict[str, str] = data.get("repos", {})
        self.stacked: dict[str, list[int]] = data.get("stacked", {})
        self.others: dict[str, list[str]] = data.get("others", {})
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
            json.dumps(
                {
                    "prs": self._prs,
                    "numbers": self.numbers,
                    "repos": self.repos,
                    "stacked": self.stacked,
                    "others": self.others,
                    "flags": self.flags,
                },
                indent=1,
            )
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
            if first and item.kind not in ("thread", "ci", "conflict") and not _recent(item, now):
                continue
            fresh.append(item)
        state["baselined"] = True
        state["seen"] = sorted(seen)[-MAX_SEEN:]
        state["pending"] = list(dict.fromkeys([*state["pending"], *[i.key for i in fresh]]))
        if len(state["items"]) > MAX_ITEMS:
            for stale in list(state["items"])[: len(state["items"]) - MAX_ITEMS]:
                state["items"].pop(stale)
        return fresh

    def discard_stale_ci(self, key: str, head: str) -> None:
        """A CI failure or conflict from an older head is not worth a builder's time; one on the current head stays."""
        state = self.pr(key)
        stale = [
            k
            for k in state["items"]
            if k.startswith(("ci:", "conflict:")) and k.split(":", 1)[1] != head[:8]
        ]
        for k in stale:
            state["items"].pop(k)
        state["pending"] = [k for k in state["pending"] if k not in stale]

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

    def forget_flags(self, key: str) -> None:
        """A PR that is no longer open needs none of its one-off notices."""
        # Exact PR only: PR #36's flags must not match PR #36269's ("...#36" is a prefix of "...#36269").
        for flag in [
            f
            for f in self.flags
            if (rest := f.split(":", 1)[-1]) == key or rest.startswith(key + ":")
        ]:
            self.flags.pop(flag)

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


def _recent(item: ReviewItem, now: float | None) -> bool:
    """A comment or review body found on the first look is still feedback when it is less than a day old."""
    try:
        created = datetime.fromisoformat(item.created_at).timestamp()
    except (ValueError, AttributeError):
        return False
    return (now or time.time()) - created < FIRST_LOOK_FRESH_SECONDS


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
    if (state or {}).get("merged"):
        return "merged"
    ready = is_ready_to_merge(state, needs_you)
    labels = (state or {}).get("labels", [])
    if (
        requeue
        and requeue.get("status") == "requeued"
        and queued(labels)
        and "blocked" not in labels
    ):
        return "re-queued"
    if requeue and requeue.get("status") == "refused" and not ready:
        return "re-queue refused"
    if ready:
        return "ready to merge"
    if queued(labels) and "blocked" not in labels:
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
        if state.get("merged"):
            return {
                "tone": "merged",
                "headline": "Merged",
                "next": "On develop. Nothing more to do on this PR.",
            }
        return {
            "tone": "wait",
            "headline": "Closed without merging",
            "next": "Reopen it or start again.",
        }
    if state.get("draft"):
        return {"tone": "wait", "headline": "Draft PR", "next": "Mark it ready for review."}
    if state.get("conflicting"):
        base = state.get("base") or "the base branch"
        return {
            "tone": "need",
            "headline": f"Merge conflicts with {base}",
            "next": "Aviator cannot merge it until they are resolved. The builder resolves them and re-queues the PR.",
        }
    if mergeability_pending(state):
        return {
            "tone": "wait",
            "headline": "GitHub is still checking for conflicts",
            "next": "It is re-checked within a minute.",
        }
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
        approvers = state.get("approvers", [])
        return {
            "tone": "need",
            "headline": f"Changes requested by {who}",
            "next": (
                f"{', '.join('@' + a for a in approvers)} approved. Once {who}'s comments are addressed, "
                "the builder dismisses the request so the approval can merge."
                if approvers
                else "The builder picks the comments up on the next check."
            ),
        }
    if unresolved:
        return {
            "tone": "wait",
            "headline": f"{unresolved} thread{'s' if unresolved != 1 else ''} still open",
            "next": "Waiting for the threads to be resolved.",
        }
    reruns = int(state.get("ciReruns") or 0)
    if ci == "failure":
        return {
            "tone": "need",
            "headline": "CI is failing",
            "next": (
                f"Re-run {reruns} time{'s' if reruns != 1 else ''} after an infrastructure failure. The builder looks at it now."
                if reruns
                else "The builder looks at the failing checks."
            ),
        }
    if ci in ("pending", "expected"):
        return {
            "tone": "work",
            "headline": "CI is re-running" if reruns else "CI is running",
            "next": (
                f"Attempt {reruns + 1} after an infrastructure failure. The builder gets it only if it fails again."
                if reruns
                else "Waiting for the checks to finish. You can review meanwhile."
            ),
        }
    in_queue = queued(labels)
    if not state.get("approved"):
        if in_queue:
            return {
                "tone": "wait",
                "headline": "Queued, waiting for a reviewer's approval",
                "next": "Out for review. Aviator merges it once it is approved and green.",
            }
        return {
            "tone": "need",
            "headline": "Ready for your review",
            "next": "Approve to add it to the merge queue, or request changes.",
        }
    if in_queue and "blocked" not in labels:
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
            "heldSince": 0,
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
        if (
            decision
            and item.get("kind") in BUILDER_KINDS
            and action
            and _after(action.get("at", ""), decision.get("at", 0))
        ):
            decision = None
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
        if state in ("queued", "delivered") and item.get("thread_id") in store_pr.get(
            "resolved_threads", []
        ):
            # Resolved on GitHub (the builder, or you): nothing is waiting, whether or not it reported back.
            state = "answered"
        rows.append(
            {
                **{
                    k: item[k]
                    for k in ("key", "kind", "author", "path", "line", "body", "url")
                    if k in item
                },
                "state": state,
                "foundAt": float(item.get("found_at") or 0),
                "reply": clean_draft(reply),
                "options": builder_options(reply)
                if item.get("kind") in BUILDER_KINDS and state == "needs_you"
                else [],
                "postedUrl": (decision or {}).get("posted_url", ""),
                "resolved": bool((decision or {}).get("resolved")),
                "githubResolved": bool(
                    item.get("thread_id")
                    and item["thread_id"] in store_pr.get("resolved_threads", [])
                ),
            }
        )
    relevant = [r for r in rows if r["state"] != "seen"]
    labels = (store_pr.get("state") or {}).get("labels", [])
    blocked_note = (
        "blocked: not re-queued"
        if "blocked" in labels
        and not queued(labels)
        and any(r["kind"] in BUILDER_KINDS and r["state"] == "fixed" for r in relevant)
        else ""
    )
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
        "blockedNote": blocked_note,
        "addressed": sum(
            1
            for r in relevant
            if r["state"] in ("fixed", "replied", "skipped", "posted", "answered")
        ),
        "needsYou": sum(1 for r in relevant if r["state"] == "needs_you"),
        "waiting": sum(1 for r in relevant if r["state"] in ("queued", "delivered")),
        "heldSince": min(
            (r["foundAt"] for r in relevant if r["state"] == "queued" and r["foundAt"]), default=0
        ),
        "items": sorted(relevant, key=lambda r: (r["state"] != "needs_you", r["key"])),
    }


PR_URL = re.compile(r"/pull/(\d+)")
BUILDER_KINDS = ("ci", "conflict")


def merged_store_pr(
    primary: dict[str, Any] | None, others: list[dict[str, Any] | None]
) -> dict[str, Any] | None:
    """The primary PR's record with the items of its stacked PRs folded in, so one view covers the stack."""
    extra = [o for o in others if o]
    if primary is None or not extra:
        return primary
    merged = dict(primary)
    for field_name in ("items", "resolutions"):
        merged[field_name] = {**primary.get(field_name, {})}
        for other in extra:
            merged[field_name].update(other.get(field_name, {}))
    for field_name in ("pending", "resolved_threads"):
        merged[field_name] = list(primary.get(field_name, []))
        for other in extra:
            merged[field_name] += [
                x for x in other.get(field_name, []) if x not in merged[field_name]
            ]
    return merged


def _after(stamp: str, epoch: float) -> bool:
    """True when the builder's ISO timestamp is later than a decision taken at the epoch time."""
    try:
        return datetime.fromisoformat(stamp).timestamp() > float(epoch)
    except (ValueError, TypeError):
        return False


OPTION_LINE = re.compile(r"^\s*\(?(\d+)[.)]\s+(.+?)\s*$")
OPTION_INLINE = re.compile(r"\((\d+)\)\s*")


def builder_options(text: str) -> list[dict[str, str]]:
    """The numbered choices in a builder's needs-you note: one per line, or inline as (1) ... (2) ...."""
    found = [OPTION_LINE.match(line) for line in text.splitlines()]
    options = [{"key": m.group(1), "label": m.group(2)} for m in found if m]
    if options:
        return options
    parts = OPTION_INLINE.split(text)
    return [
        {"key": parts[i], "label": parts[i + 1].strip().rstrip(".,;")}
        for i in range(1, len(parts) - 1, 2)
        if parts[i + 1].strip()
    ]
