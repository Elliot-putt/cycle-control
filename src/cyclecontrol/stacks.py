"""Works out which sub-issues of a parent are ready to start, and from which base branch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cyclecontrol.models import Issue

Kind = Literal["done", "pr_open", "running", "queued", "ready", "blocked"]


@dataclass(frozen=True)
class PrInfo:
    number: int
    url: str
    branch: str
    state: str
    base: str = ""
    head_sha: str = ""


@dataclass(frozen=True)
class StackEntry:
    pr: int
    issue: str
    branch: str
    after: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChildState:
    issue_id: str
    kind: Kind
    base: str = ""
    waiting_on: tuple[str, ...] = ()
    pr: PrInfo | None = None


def read_plan_stack(plan: dict[str, Any] | None) -> list[StackEntry]:
    entries: list[StackEntry] = []
    for raw in (plan or {}).get("stack", []):
        if not isinstance(raw, dict) or not raw.get("issue"):
            continue
        entries.append(
            StackEntry(
                pr=int(raw.get("pr", 0)),
                issue=str(raw["issue"]),
                branch=str(raw.get("branch", "")),
                after=tuple(int(n) for n in raw.get("after", [])),
            )
        )
    return entries


def branch_of(issue: Issue, plan_stack: list[StackEntry]) -> str:
    """The branch that holds an issue's work: the plan's record first, then Linear's branch name."""
    entry = next((e for e in plan_stack if e.issue == issue.id and e.branch), None)
    return entry.branch if entry else issue.branch


def branches_to_check(children: list[Issue], plan_stack: list[StackEntry]) -> dict[str, str]:
    return {child.id: branch_of(child, plan_stack) for child in children}


def _blocker_ids(child: Issue, plan_stack: list[StackEntry]) -> list[tuple[str, str]]:
    """(issue id, Linear status type) of what must finish first; the plan's `after` fills in when Linear has none."""
    if child.blocked_by:
        return [(b.id, b.status_type) for b in child.blocked_by]
    entry = next((e for e in plan_stack if e.issue == child.id), None)
    if not entry:
        return []
    by_pr = {e.pr: e.issue for e in plan_stack}
    return [(by_pr[n], "") for n in entry.after if n in by_pr and by_pr[n] != child.id]


def _depth(
    child: Issue,
    by_id: dict[str, Issue],
    plan_stack: list[StackEntry],
    seen: frozenset[str] = frozenset(),
) -> int:
    if child.id in seen:
        return 0
    deeper = [
        _depth(by_id[bid], by_id, plan_stack, seen | {child.id})
        for bid, _ in _blocker_ids(child, plan_stack)
        if bid in by_id
    ]
    return 1 + max(deeper, default=-1) if deeper else 0


def resolve_stack(
    children: list[Issue],
    prs: dict[str, PrInfo | None],
    running: set[str],
    queued: set[str],
    default_base: str,
    plan_stack: list[StackEntry] | None = None,
) -> list[ChildState]:
    """One state per child, ordered by dependency then by plan order, then by creation."""
    plan = plan_stack or []
    by_id = {c.id: c for c in children}
    plan_order = {e.issue: e.pr for e in plan}

    def merged(issue_id: str) -> bool:
        pr = prs.get(issue_id)
        return bool(pr and pr.state == "MERGED")

    def is_done(issue_id: str, linear_type: str) -> bool:
        if issue_id in by_id:
            return by_id[issue_id].status_type == "completed" or merged(issue_id)
        return linear_type == "completed"

    def open_pr(issue_id: str) -> PrInfo | None:
        pr = prs.get(issue_id)
        return pr if pr and pr.state == "OPEN" else None

    def state_of(child: Issue) -> ChildState:
        pr = prs.get(child.id)
        if child.status_type == "completed" or merged(child.id):
            return ChildState(child.id, "done", pr=pr)
        if child.id in running:
            return ChildState(child.id, "running", pr=pr)
        if child.id in queued:
            return ChildState(child.id, "queued", pr=pr)
        if open_pr(child.id):
            return ChildState(child.id, "pr_open", pr=pr)
        blockers = _blocker_ids(child, plan)
        if not blockers:
            return ChildState(child.id, "ready", base=default_base)
        pending = [bid for bid, kind in blockers if not is_done(bid, kind)]
        if not pending:
            return ChildState(child.id, "ready", base=default_base)
        if len(blockers) == 1 and (stack_pr := open_pr(pending[0])):
            return ChildState(child.id, "ready", base=stack_pr.branch)
        return ChildState(child.id, "blocked", waiting_on=tuple(pending))

    def sort_key(child: Issue) -> tuple[int, int, str, str]:
        number = child.id.rsplit("-", 1)[-1]
        return (
            _depth(child, by_id, plan),
            plan_order.get(child.id, 10_000),
            child.created_at,
            number.zfill(8),
        )

    return [state_of(child) for child in sorted(children, key=sort_key)]
