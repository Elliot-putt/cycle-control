import pytest

from cyclecontrol.comments import (
    CommentStore,
    approval_line,
    comments_view,
    feedback_text,
    is_ready_to_merge,
    lane_label,
    parse_pr,
    pr_state,
    read_requeue,
    review_status,
)
from tests.test_comments import c, pr, thread


def rv(login, state, hour, typename="User"):
    return {
        "databaseId": hour,
        "author": {"login": login, "__typename": typename},
        "state": state,
        "body": "",
        "url": "",
        "submittedAt": f"2026-10-19T{hour:02d}:00:00Z",
    }


def status(*reviews, author="owner"):
    return review_status(pr(reviews=list(reviews), author=author))


@pytest.mark.parametrize(
    ("reviews", "approved", "blocking"),
    [
        ([rv("rio", "APPROVED", 10)], True, ()),
        ([rv("rio", "APPROVED", 10), rv("rio", "COMMENTED", 11)], True, ()),
        ([rv("rio", "COMMENTED", 10)], False, ()),
        ([rv("rio", "CHANGES_REQUESTED", 10)], False, ("rio",)),
        ([rv("rio", "APPROVED", 10), rv("rio", "CHANGES_REQUESTED", 11)], False, ("rio",)),
        ([rv("rio", "CHANGES_REQUESTED", 10), rv("rio", "APPROVED", 11)], True, ()),
        ([rv("rio", "CHANGES_REQUESTED", 10), rv("rio", "COMMENTED", 11)], False, ()),
        ([rv("rio", "APPROVED", 10), rv("sam", "CHANGES_REQUESTED", 11)], False, ("sam",)),
        ([rv("rio", "APPROVED", 10), rv("sam", "COMMENTED", 11)], True, ()),
        ([rv("aviator-app", "APPROVED", 10, "Bot")], False, ()),
        ([rv("owner", "APPROVED", 10)], False, ()),
        ([rv("rio", "APPROVED", 10), rv("rio", "PENDING", 11)], True, ()),
        ([], False, ()),
    ],
)
def test_the_team_approval_convention(reviews, approved, blocking):
    result = status(*reviews)
    assert (result.approved, result.blocking) == (approved, blocking)


def test_a_later_review_wins_whatever_order_github_lists_them_in():
    assert status(rv("rio", "COMMENTED", 12), rv("rio", "APPROVED", 10)).approved is True
    assert status(rv("rio", "APPROVED", 12), rv("rio", "CHANGES_REQUESTED", 10)).approved is True


def test_the_real_shape_of_1479_takes_the_approved_path():
    result = status(rv("rio-whatley", "APPROVED", 11), rv("rio-whatley", "COMMENTED", 12))
    assert result.approved and result.approvers == ("rio-whatley",)


def full_pr(**over):
    base = pr(
        reviews=[rv("rio", "APPROVED", 10)],
        threads=[
            thread(
                "T1", [c(1, "rio", "x"), c(2, "owner", "fixed <!-- spp-builder -->")], resolved=True
            )
        ],
    )
    base["labels"] = {"nodes": []}
    base["isDraft"] = False
    base["commits"] = {
        "nodes": [{"commit": {"oid": "H", "statusCheckRollup": {"state": "SUCCESS"}}}]
    }
    base.update(over)
    return base


def test_pr_state_reads_ci_labels_and_unresolved_threads():
    state = pr_state(
        full_pr(
            labels={"nodes": [{"name": "blocked"}]},
            reviewThreads={
                "nodes": [
                    thread("T1", [c(1, "rio", "x")]),
                    thread("T2", [c(2, "rio", "y")], resolved=True),
                ]
            },
        )
    )
    assert (
        state["ci"],
        state["unresolved"],
        state["labels"],
        state["approved"],
        state["open"],
    ) == ("SUCCESS", 1, ["blocked"], True, True)
    assert pr_state(full_pr(commits={"nodes": []}))["ci"] == ""


@pytest.mark.parametrize(
    ("change", "ready"),
    [
        ({}, True),
        ({"state": "CLOSED"}, False),
        ({"isDraft": True}, False),
        ({"reviews": {"nodes": []}}, False),
        ({"reviews": {"nodes": [rv("rio", "CHANGES_REQUESTED", 10)]}}, False),
        ({"reviewThreads": {"nodes": [thread("T1", [c(1, "rio", "x")])]}}, False),
        (
            {
                "commits": {
                    "nodes": [{"commit": {"oid": "H", "statusCheckRollup": {"state": "FAILURE"}}}]
                }
            },
            False,
        ),
        (
            {
                "commits": {
                    "nodes": [{"commit": {"oid": "H", "statusCheckRollup": {"state": "PENDING"}}}]
                }
            },
            False,
        ),
        ({"labels": {"nodes": [{"name": "mergequeue"}]}}, False),
        ({"labels": {"nodes": [{"name": "blocked"}]}}, False),
    ],
)
def test_ready_to_merge_needs_every_condition(change, ready):
    assert is_ready_to_merge(pr_state(full_pr(**change))) is ready


def test_a_needs_you_item_stops_it_being_ready_to_merge():
    state = pr_state(full_pr())
    assert is_ready_to_merge(state, needs_you=0) and not is_ready_to_merge(state, needs_you=1)
    assert is_ready_to_merge(None) is False


def test_the_feedback_file_tells_the_builder_whether_to_re_request_review():
    facts, items = parse_pr(
        full_pr(reviewThreads={"nodes": [thread("T1", [c(1, "sam", "Rename")])]})
    )
    approved = feedback_text("o/r", facts, items, pr_state(full_pr()))
    assert "Approval: approved (@rio)" in approved and "Do not re-request review" in approved
    blocked = pr_state(full_pr(reviews={"nodes": [rv("rio", "CHANGES_REQUESTED", 10)]}))
    line = approval_line(blocked, items)
    assert (
        "not approved (changes requested by @rio)" in line
        and "re-request review from: @rio, @sam" in line
    )
    nobody = approval_line(pr_state(full_pr(reviews={"nodes": []})), items)
    assert "no approval yet" in nobody and "@sam" in nobody
    assert approval_line(None, items) == ""


def test_lane_tags():
    ready = pr_state(full_pr())
    queued = pr_state(full_pr(labels={"nodes": [{"name": "mergequeue"}]}))
    blocked = pr_state(full_pr(labels={"nodes": [{"name": "mergequeue"}, {"name": "blocked"}]}))
    assert lane_label(ready, 0, None) == "ready to merge"
    assert lane_label(ready, 1, None) == ""
    assert lane_label(queued, 0, None) == "in merge queue"
    assert lane_label(queued, 0, {"status": "requeued"}) == "re-queued"
    assert lane_label(blocked, 0, {"status": "requeued"}) == ""
    assert lane_label(blocked, 0, {"status": "refused"}) == "re-queue refused"
    assert lane_label(ready, 0, {"status": "refused"}) == "ready to merge"
    assert lane_label(None, 0, None) == ""


def test_the_requeue_note_is_read_from_the_git_dir(tmp_path):
    assert read_requeue(tmp_path) is None
    (tmp_path / "spp-requeue.json").write_text('{"status":"requeued","pr":7,"reasons":[],"at":"t"}')
    assert read_requeue(tmp_path)["status"] == "requeued"
    (tmp_path / "spp-requeue.json").write_text("nope")
    assert read_requeue(tmp_path) is None


def test_the_view_carries_the_lane_and_status(tmp_path):
    store = CommentStore(tmp_path / "c.json")
    store.record("k", [])
    store.set_state("k", pr_state(full_pr()))
    view = comments_view(store.peek("k"), {}, None)
    assert view["lane"] == "ready to merge" and view["status"]["approved"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Draft reply (not posted): The wording is kept. <!-- spp-builder -->",
            "The wording is kept.",
        ),
        ("Draft reply (not posted):\nKept on purpose.", "Kept on purpose."),
        ("Draft: Kept.", "Kept."),
        ("DRAFTED REPLY: Kept.", "Kept."),
        ("Proposed reply: Kept.", "Kept."),
        ("Draft (not posted): Kept.", "Kept."),
        ("Kept on purpose. <!-- spp-builder -->", "Kept on purpose."),
        ("Draft the plan later: not a label", "Draft the plan later: not a label"),
        ("  plain reply  ", "plain reply"),
        ("", ""),
    ],
)
def test_a_draft_is_cleaned_of_its_label_and_marker_before_it_is_shown_or_posted(text, expected):
    from cyclecontrol.comments import clean_draft

    assert clean_draft(text) == expected


def test_a_blocked_pr_that_is_otherwise_ready_is_a_requeue_candidate():
    from cyclecontrol.comments import is_requeue_candidate

    blocked = {"labels": {"nodes": [{"name": "blocked"}]}}
    assert is_requeue_candidate(pr_state(full_pr(**blocked))) is True
    assert is_requeue_candidate(pr_state(full_pr())) is False, "not blocked"
    assert is_requeue_candidate(pr_state(full_pr(**blocked)), needs_you=1) is False
    assert is_requeue_candidate(pr_state(full_pr(reviews={"nodes": [], **blocked}))) is False
    assert (
        is_requeue_candidate(
            pr_state(full_pr(reviews={"nodes": [rv("rio", "CHANGES_REQUESTED", 10)]}, **blocked))
        )
        is False
    )
    assert (
        is_requeue_candidate(
            pr_state(
                full_pr(reviewThreads={"nodes": [thread("T1", [c(1, "rio", "x")])]}, **blocked)
            )
        )
        is False
    )
    red = {
        "commits": {"nodes": [{"commit": {"oid": "H", "statusCheckRollup": {"state": "FAILURE"}}}]}
    }
    assert is_requeue_candidate(pr_state(full_pr(**blocked, **red))) is False
    assert is_requeue_candidate(pr_state(full_pr(state="CLOSED", **blocked))) is False
    assert is_requeue_candidate(None) is False
    assert pr_state(full_pr(headRefOid="abc"))["head"] == "abc"


def _state(**over):
    base = {
        "open": True,
        "draft": False,
        "approved": True,
        "approvers": ["rio"],
        "blocking": [],
        "ci": "SUCCESS",
        "unresolved": 0,
        "labels": [],
    }
    return {**base, **over}


@pytest.mark.parametrize(
    ("state", "needs", "waiting", "requeue", "headline"),
    [
        (_state(), 1, 0, None, "1 review comment needs your decision"),
        (_state(), 0, 2, None, "Builder is working through 2 review comments"),
        (_state(approved=False, blocking=["rio"]), 0, 0, None, "Changes requested by @rio"),
        (_state(unresolved=2), 0, 0, None, "2 threads still open"),
        (_state(ci="PENDING"), 0, 0, None, "CI is running"),
        (_state(ci="FAILURE"), 0, 0, None, "CI is failing"),
        (_state(approved=False), 0, 0, None, "Ready for your review"),
        (
            _state(approved=False, labels=["mergequeue"]),
            0,
            0,
            None,
            "Queued, waiting for a reviewer's approval",
        ),
        (_state(labels=["mergequeue"]), 0, 0, None, "In the merge queue"),
        (_state(labels=["blocked"]), 0, 0, None, "Blocked by Aviator, approved and clean"),
        (
            _state(labels=["blocked"]),
            0,
            0,
            {"status": "refused", "reasons": ["CI is failure"]},
            "Blocked by Aviator, re-queue refused",
        ),
        (_state(), 0, 0, None, "Approved and ready to merge"),
        (_state(open=False, merged=True), 0, 0, None, "Merged"),
        (_state(open=False), 0, 0, None, "Closed without merging"),
    ],
)
def test_review_stage_headline(state, needs, waiting, requeue, headline):
    from cyclecontrol.comments import review_stage

    assert review_stage(state, needs, waiting, requeue)["headline"] == headline


def test_review_stage_is_empty_without_a_pr_state():
    from cyclecontrol.comments import review_stage

    assert review_stage(None, 0, 0, None) is None


def test_a_merged_pr_has_the_merged_lane_and_tone():
    from cyclecontrol.comments import lane_label, review_stage

    merged = _state(open=False, merged=True, labels=["mergequeue"])
    assert lane_label(merged, 0, {"status": "requeued"}) == "merged"
    assert review_stage(merged, 0, 0, None)["tone"] == "merged"


def test_ci_ignores_the_merge_bot_and_coderabbit_but_not_real_checks():
    from cyclecontrol.comments import effective_ci

    def run(name, status="COMPLETED", conclusion="SUCCESS"):
        return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}

    def status(context, state):
        return {"__typename": "StatusContext", "context": context, "state": state}

    green = [run("ci/circleci: lint"), status("ci/circleci: test", "SUCCESS")]
    bots = [status("aviator/checks", "PENDING"), status("CodeRabbit", "PENDING")]
    assert effective_ci({"state": "PENDING", "contexts": {"nodes": green + bots}}) == "SUCCESS"
    pending = [run("lint", status="IN_PROGRESS", conclusion=None)]
    assert effective_ci({"state": "PENDING", "contexts": {"nodes": pending + bots}}) == "PENDING"
    failing = [run("lint", conclusion="FAILURE")]
    assert effective_ci({"state": "FAILURE", "contexts": {"nodes": failing}}) == "FAILURE"
    assert effective_ci(None) == ""


def _failing(**over):
    return _state(ci="FAILURE", failing=["ci/circleci: test-jest"], head="aef6849f0000", **over)


def test_a_failing_ci_on_an_open_pr_becomes_an_item_for_the_builder():
    from cyclecontrol.comments import PrFacts, ci_item, feedback_text

    item = ci_item(_failing(), "https://github.com/o/r", 7)
    assert item.key == "ci:aef6849f" and "test-jest" in item.body
    text = feedback_text("o/r", PrFacts(7, "OPEN", "me", "feature/x", ""), [item])
    assert "ci:aef6849f" in text and "get_build_failure_logs" in text


@pytest.mark.parametrize("over", [{"open": False}, {"draft": True}, {"failing": []}])
def test_no_ci_item_when_closed_draft_or_nothing_failed(over):
    from cyclecontrol.comments import ci_item

    assert ci_item({**_failing(), **over}, "https://github.com/o/r", 7) is None


def test_a_ci_item_for_an_old_head_is_discarded(tmp_path):
    from cyclecontrol.comments import CommentStore, ci_item

    store = CommentStore(tmp_path / "c.json")
    old = ci_item(_failing(), "https://github.com/o/r", 7)
    assert store.record("k", [old]) == [old]
    store.discard_stale_ci("k", "ci:other")
    assert store.pr("k")["items"] == {} and store.pending_items("k") == []
