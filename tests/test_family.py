from cyclecontrol.board import Board
from cyclecontrol.family import FamilyService
from cyclecontrol.models import Blocker, Cycle, Issue
from cyclecontrol.sessions import SessionManager
from cyclecontrol.stacks import PrInfo
from tests.test_sessions import settings_for


def item(
    issue_id, parent="", children=False, blockers=(), team="ABC", mine=True, status_type="backlog"
):
    return Issue(
        issue_id,
        f"Title {issue_id}",
        f"https://x/{issue_id}",
        f"{team.lower()}/{issue_id.lower()}",
        "Todo",
        status_type,
        "E",
        False,
        mine,
        team_key=team,
        parent_id=parent,
        has_children=children,
        blocked_by=tuple(Blocker(b, "backlog") for b in blockers),
    )


def build(tmp_path, issues):
    settings = settings_for(tmp_path)
    board = Board(settings)
    board.issues = issues
    board.cycle = Cycle(1, 1, 0, 1)
    sessions = SessionManager(settings)
    return FamilyService(settings, board, sessions), sessions, settings


def test_groups_show_ready_blocked_and_base_for_a_made_up_parent(tmp_path):
    family, _, _ = build(
        tmp_path,
        [
            item("ABC-10", children=True),
            item("ABC-11", parent="ABC-10"),
            item("ABC-12", parent="ABC-10"),
            item("ABC-13", parent="ABC-10", blockers=("ABC-11", "ABC-12")),
            item("ABC-99"),
        ],
    )
    (group,) = family.groups()
    assert group["id"] == "ABC-10" and group["teamKey"] == "ABC"
    kinds = {c["id"]: (c["kind"], c["startable"], c["startReason"]) for c in group["children"]}
    assert kinds["ABC-11"] == ("ready", True, "")
    assert kinds["ABC-12"] == ("ready", True, "")
    assert kinds["ABC-13"] == ("blocked", False, "Waiting on ABC-11, ABC-12.")
    assert group["readyCount"] == 2 and group["doneCount"] == 0
    assert [c["base"] for c in group["children"] if c["kind"] == "ready"] == ["main", "main"]


def test_a_child_stacks_on_its_blockers_open_pr_and_pr_info_reaches_the_group(tmp_path):
    family, _, _ = build(
        tmp_path,
        [
            item("ABC-10", children=True),
            item("ABC-11", parent="ABC-10"),
            item("ABC-12", parent="ABC-10", blockers=("ABC-11",)),
        ],
    )
    family._prs["ABC-11"] = PrInfo(
        7, "https://example.com/pull/7", "abc/abc-11", "OPEN", "main", "sha"
    )
    kinds = {c["id"]: c for c in family.groups()[0]["children"]}
    assert kinds["ABC-11"]["kind"] == "pr_open" and kinds["ABC-11"]["prNumber"] == 7
    assert kinds["ABC-12"]["kind"] == "ready" and kinds["ABC-12"]["base"] == "abc/abc-11"
    assert family.groups()[0]["doneCount"] == 1


def test_children_of_an_unmapped_team_are_not_startable_and_say_why(tmp_path):
    family, _, _ = build(
        tmp_path,
        [item("WEB-10", children=True, team="WEB"), item("WEB-11", parent="WEB-10", team="WEB")],
    )
    group = family.groups()[0]
    assert group["startReason"] == "No repo mapped for team WEB."
    assert group["children"][0]["startable"] is False
    assert group["children"][0]["startReason"] == "No repo mapped for team WEB."
    assert group["readyCount"] == 0


def test_queued_children_show_as_queued(tmp_path):
    family, sessions, _ = build(
        tmp_path, [item("ABC-10", children=True), item("ABC-11", parent="ABC-10")]
    )
    sessions._queue.append({"issue": {"id": "ABC-11"}, "mode": "plan", "base": "main"})
    assert family.groups()[0]["children"][0]["kind"] == "queued"


def test_a_pr_in_another_repo_than_the_teams_still_counts_as_open(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cyclecontrol import family as family_module
    from cyclecontrol.github import PrState

    svc, sessions, _ = build(
        tmp_path, [item("ABC-10", children=True), item("ABC-11", parent="ABC-10")]
    )
    session = SimpleNamespace(labelled=["https://github.com/Org/other-repo/pull/9"], pr_urls=list)
    monkeypatch.setattr(sessions, "get", lambda issue_id: session)
    seen = []

    def fake_find(repo, branch):
        seen.append(repo)
        if repo == "Org/other-repo":
            return PrState(
                9, "https://github.com/Org/other-repo/pull/9", branch, "OPEN", "main", "s"
            )
        return None

    monkeypatch.setattr(family_module, "find_pr_any_state", fake_find)
    monkeypatch.setattr(family_module, "find_pr_by_number", lambda repo, number: None)
    svc.refresh()
    (group,) = svc.groups()
    child = next(c for c in group["children"] if c["id"] == "ABC-11")
    assert child["kind"] == "pr_open" and child["prNumber"] == 9
    assert "Org/other-repo" in seen


def test_a_finished_builder_with_no_link_is_found_in_the_known_repos(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cyclecontrol import family as family_module
    from cyclecontrol.github import PrState

    svc, sessions, _ = build(
        tmp_path, [item("ABC-10", children=True), item("ABC-11", parent="ABC-10")]
    )
    session = SimpleNamespace(labelled=[], pr_urls=list)
    monkeypatch.setattr(sessions, "get", lambda issue_id: session)

    def fake_find(repo, branch):
        if repo == "AgentSoftware/street":
            return PrState(
                5, "https://github.com/AgentSoftware/street/pull/5", branch, "MERGED", "main", "s"
            )
        return None

    monkeypatch.setattr(family_module, "find_pr_any_state", fake_find)
    monkeypatch.setattr(family_module, "find_pr_by_number", lambda repo, number: None)
    svc.refresh()
    (group,) = svc.groups()
    child = next(c for c in group["children"] if c["id"] == "ABC-11")
    assert child["kind"] == "done" and child["prNumber"] == 5


def test_a_pr_on_a_differently_named_branch_is_found_by_its_recorded_number(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cyclecontrol import family as family_module
    from cyclecontrol.github import PrState

    svc, sessions, _ = build(
        tmp_path, [item("ABC-10", children=True), item("ABC-11", parent="ABC-10")]
    )
    url = "https://github.com/AgentSoftware/street/pull/36646"
    session = SimpleNamespace(labelled=[url], pr_urls=list)
    monkeypatch.setattr(sessions, "get", lambda issue_id: session)
    monkeypatch.setattr(family_module, "find_pr_any_state", lambda repo, branch: None)
    monkeypatch.setattr(
        family_module,
        "find_pr_by_number",
        lambda repo, number: PrState(number, url, "abc/abc-11-street", "OPEN", "develop", "s"),
    )
    svc.refresh()
    (group,) = svc.groups()
    child = next(c for c in group["children"] if c["id"] == "ABC-11")
    assert child["kind"] == "pr_open" and child["prNumber"] == 36646
