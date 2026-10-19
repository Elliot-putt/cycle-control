from cyclecontrol.models import Blocker, Issue
from cyclecontrol.stacks import PrInfo, StackEntry, branch_of, read_plan_stack, resolve_stack


def child(issue_id, blockers=(), status_type="backlog", branch=None, created="2026-01-01"):
    return Issue(
        id=issue_id,
        title=f"Title {issue_id}",
        url="",
        branch=branch or f"team/{issue_id.lower()}-work",
        status="Backlog",
        status_type=status_type,
        assignee="",
        in_cycle=False,
        is_mine=True,
        team_key=issue_id.split("-")[0],
        parent_id="ABC-10",
        blocked_by=tuple(Blocker(b, "backlog") for b in blockers),
        created_at=created,
    )


def pr(number, branch, state="OPEN"):
    return PrInfo(number, f"https://example.com/pull/{number}", branch, state, "main", "abc")


def kinds(states):
    return {s.issue_id: (s.kind, s.base, s.waiting_on) for s in states}


def test_three_children_where_one_depends_on_the_other_two():
    children = [child("ABC-11"), child("ABC-12"), child("ABC-13", blockers=("ABC-11", "ABC-12"))]
    result = resolve_stack(children, {}, set(), set(), "main")
    assert kinds(result) == {
        "ABC-11": ("ready", "main", ()),
        "ABC-12": ("ready", "main", ()),
        "ABC-13": ("blocked", "", ("ABC-11", "ABC-12")),
    }
    assert [s.issue_id for s in result][-1] == "ABC-13"


def test_a_child_with_two_blockers_waits_for_both_to_merge_not_just_open():
    children = [child("ABC-11"), child("ABC-12"), child("ABC-13", blockers=("ABC-11", "ABC-12"))]
    prs = {
        "ABC-11": pr(1, "team/abc-11-work", "MERGED"),
        "ABC-12": pr(2, "team/abc-12-work", "OPEN"),
    }
    assert kinds(resolve_stack(children, prs, set(), set(), "main"))["ABC-13"] == (
        "blocked",
        "",
        ("ABC-12",),
    )
    prs["ABC-12"] = pr(2, "team/abc-12-work", "MERGED")
    assert kinds(resolve_stack(children, prs, set(), set(), "main"))["ABC-13"] == (
        "ready",
        "main",
        (),
    )


def test_a_child_with_one_blocker_stacks_on_the_blockers_open_pr_branch():
    children = [child("ABC-11"), child("ABC-12", blockers=("ABC-11",))]
    assert kinds(resolve_stack(children, {}, set(), set(), "main"))["ABC-12"] == (
        "blocked",
        "",
        ("ABC-11",),
    )
    prs = {"ABC-11": pr(1, "team/abc-11-work")}
    result = kinds(resolve_stack(children, prs, set(), set(), "main"))
    assert result["ABC-11"][0] == "pr_open"
    assert result["ABC-12"] == ("ready", "team/abc-11-work", ())
    prs = {"ABC-11": pr(1, "team/abc-11-work", "MERGED")}
    assert kinds(resolve_stack(children, prs, set(), set(), "main"))["ABC-12"] == (
        "ready",
        "main",
        (),
    )


def test_running_queued_and_done_children_are_never_ready_again():
    children = [child("ABC-11"), child("ABC-12"), child("ABC-13", status_type="completed")]
    result = kinds(resolve_stack(children, {}, {"ABC-11"}, {"ABC-12"}, "main"))
    assert [result[i][0] for i in ("ABC-11", "ABC-12", "ABC-13")] == ["running", "queued", "done"]


def test_a_closed_unmerged_pr_does_not_count_as_open_or_done():
    children = [child("ABC-11"), child("ABC-12", blockers=("ABC-11",))]
    prs = {"ABC-11": pr(1, "team/abc-11-work", "CLOSED")}
    result = kinds(resolve_stack(children, prs, set(), set(), "main"))
    assert result["ABC-11"][0] == "ready" and result["ABC-12"][0] == "blocked"


def test_plan_stack_names_the_branch_and_fills_in_missing_linear_relations():
    plan = {
        "stack": [
            {"pr": 1, "issue": "ABC-11", "branch": "team/built-by-parent", "after": []},
            {"pr": 2, "issue": "ABC-12", "branch": "team/abc-12-work", "after": [1]},
        ]
    }
    stack = read_plan_stack(plan)
    children = [child("ABC-11"), child("ABC-12")]
    assert branch_of(children[0], stack) == "team/built-by-parent"
    assert branch_of(children[1], stack) == "team/abc-12-work"
    prs = {"ABC-11": pr(5, "team/built-by-parent")}
    result = kinds(resolve_stack(children, prs, set(), set(), "main", stack))
    assert result["ABC-11"][0] == "pr_open"
    assert result["ABC-12"] == ("ready", "team/built-by-parent", ())


def test_a_cycle_of_blockers_never_starts_and_does_not_hang():
    children = [child("ABC-11", blockers=("ABC-12",)), child("ABC-12", blockers=("ABC-11",))]
    assert {s.kind for s in resolve_stack(children, {}, set(), set(), "main")} == {"blocked"}


def test_an_external_blocker_outside_the_family_is_respected():
    blocked = child("ABC-11")
    blocked = Issue(**{**blocked.__dict__, "blocked_by": (Blocker("OTHER-5", "started"),)})
    assert kinds(resolve_stack([blocked], {}, set(), set(), "main"))["ABC-11"] == (
        "blocked",
        "",
        ("OTHER-5",),
    )
    finished = Issue(**{**blocked.__dict__, "blocked_by": (Blocker("OTHER-5", "completed"),)})
    assert kinds(resolve_stack([finished], {}, set(), set(), "main"))["ABC-11"][0] == "ready"


def test_order_follows_dependencies_then_plan_order():
    plan = [StackEntry(2, "ABC-12", ""), StackEntry(1, "ABC-11", "")]
    children = [child("ABC-13", blockers=("ABC-12",)), child("ABC-12"), child("ABC-11")]
    order = [s.issue_id for s in resolve_stack(children, {}, set(), set(), "main", plan)]
    assert order == ["ABC-11", "ABC-12", "ABC-13"]


def test_the_five_pr_stack_shape_without_any_hard_coded_names():
    children = [
        child("XYZ-101"),
        child("XYZ-102", blockers=("XYZ-101",)),
        child("XYZ-103", blockers=("XYZ-101",)),
        child("XYZ-104", blockers=("XYZ-101",)),
        child("XYZ-105", blockers=("XYZ-102", "XYZ-103", "XYZ-104")),
    ]
    prs = {"XYZ-101": pr(9, "other/parent-branch")}
    result = kinds(resolve_stack(children, prs, set(), set(), "trunk"))
    assert result["XYZ-101"][0] == "pr_open"
    assert [result[i] for i in ("XYZ-102", "XYZ-103", "XYZ-104")] == [
        ("ready", "other/parent-branch", ())
    ] * 3
    assert result["XYZ-105"] == ("blocked", "", ("XYZ-102", "XYZ-103", "XYZ-104"))
