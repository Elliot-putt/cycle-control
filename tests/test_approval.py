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
