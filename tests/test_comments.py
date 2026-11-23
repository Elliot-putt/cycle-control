import json
import time

import pytest

from cyclecontrol import commentwatch
from cyclecontrol.comments import (
    MARKER,
    CommentStore,
    ReviewItem,
    comments_view,
    feedback_text,
    parse_pr,
    read_actions,
)
from cyclecontrol.commentwatch import CommentWatcher
from cyclecontrol.github import GithubError
from cyclecontrol.sessions import SessionManager, read_registry
from tests.test_sessions import add_session, issue, settings_for


def c(db_id, author, body, created="2026-10-19T10:00:00Z"):
    return {
        "databaseId": db_id,
        "author": {"login": author},
        "body": body,
        "url": f"https://x/{db_id}",
        "createdAt": created,
    }


def thread(thread_id, comments, resolved=False, path="app/A.php", line=3):
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": {"nodes": comments},
    }


def pr(threads=(), comments=(), reviews=(), state="OPEN", author="owner"):
    return {
        "number": 7,
        "state": state,
        "headRefName": "feature/x",
        "reviewDecision": "",
        "author": {"login": author},
        "reviewThreads": {"nodes": list(threads)},
        "comments": {"nodes": list(comments)},
        "reviews": {"nodes": list(reviews)},
    }


def review(db_id, author, state, body):
    return {
        "databaseId": db_id,
        "author": {"login": author},
        "state": state,
        "body": body,
        "url": "https://x/r",
        "submittedAt": "2026-10-19T10:00:00Z",
    }


def keys(items):
    return [i.key for i in items]


def test_unresolved_threads_waiting_on_the_builder_become_items_with_their_conversation():
    facts, items = parse_pr(
        pr(
            threads=[
                thread(
                    "T1",
                    [
                        c(1, "sam", "Rename this"),
                        c(2, "owner", f"Done {MARKER}"),
                        c(3, "sam", "Still unclear"),
                    ],
                ),
                thread("T2", [c(4, "sam", "old"), c(5, "owner", f"Fixed {MARKER}")]),
                thread("T3", [c(6, "sam", "done already")], resolved=True),
                thread("T4", [c(7, "github-actions[bot]", "lint failed")]),
                thread("T5", [c(8, "coderabbitai[bot]", "Use a constant here")]),
            ]
        )
    )
    assert (facts.number, facts.state, facts.author) == (7, "OPEN", "owner")
    assert keys(items) == ["t:3", "t:8"]
    first = items[0]
    assert (
        first.thread_id == "T1"
        and first.reply_to == 1
        and first.path == "app/A.php"
        and first.line == 3
    )
    assert (
        "@sam: Rename this" in first.body
        and "builder: Done" in first.body
        and MARKER not in first.body
    )
    assert items[1].is_coderabbit and not items[1].is_owner


def test_comments_skip_the_builders_own_bots_and_coderabbits_walkthrough():
    _, items = parse_pr(
        pr(
            comments=[
                c(10, "owner", f"Fixed {MARKER}"),
                c(11, "github-actions[bot]", "deployed"),
                c(
                    12,
                    "coderabbitai[bot]",
                    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n## Walkthrough",
                ),
                c(13, "sam", "Is this in the spec?"),
                c(14, "owner", "Please also handle the empty case"),
                c(15, "coderabbitai[bot]", "Actionable: this can throw"),
            ]
        )
    )
    assert keys(items) == ["c:13", "c:14"]
    assert items[1].is_owner and not items[0].is_owner


def test_review_bodies_count_for_requested_changes_and_never_for_coderabbit():
    _, items = parse_pr(
        pr(
            reviews=[
                review(20, "sam", "APPROVED", "LGTM"),
                review(21, "sam", "CHANGES_REQUESTED", "Please add a test"),
                review(22, "sam", "COMMENTED", ""),
                review(
                    23,
                    "coderabbitai[bot]",
                    "COMMENTED",
                    "**Actionable comments posted: 2**\n<details><summary>Nitpick comments (1)</summary>",
                ),
                review(24, "coderabbitai[bot]", "COMMENTED", "Looks fine overall"),
                review(25, "owner", "COMMENTED", f"Replied {MARKER}"),
            ]
        )
    )
    assert keys(items) == ["r:21"]


def test_the_first_look_delivers_unresolved_threads_but_baselines_old_comments(tmp_path):
    store = CommentStore(tmp_path / "c.json")
    _, items = parse_pr(
        pr(threads=[thread("T1", [c(1, "sam", "Rename")])], comments=[c(13, "sam", "old question")])
    )
    fresh = store.record("o/r#7", items)
    assert keys(fresh) == ["t:1"]
    assert keys(store.pending_items("o/r#7")) == ["t:1"]
    assert store.record("o/r#7", items) == []


def test_only_new_items_wake_the_builder_and_a_repeat_after_a_reply_counts_as_new(tmp_path):
    store = CommentStore(tmp_path / "c.json")
    first = [thread("T1", [c(1, "sam", "Rename")])]
    store.record("k", parse_pr(pr(threads=first))[1])
    store.mark_delivered("k")
    assert store.record("k", parse_pr(pr(threads=first))[1]) == []
    answered = [thread("T1", [c(1, "sam", "Rename"), c(2, "owner", f"Renamed {MARKER}")])]
    assert store.record("k", parse_pr(pr(threads=answered))[1]) == []
    repeated = [
        thread(
            "T1",
            [
                c(1, "sam", "Rename"),
                c(2, "owner", f"Renamed {MARKER}"),
                c(3, "sam", "Not what I meant"),
            ],
        )
    ]
    assert keys(store.record("k", parse_pr(pr(threads=repeated))[1])) == ["t:3"]
    store.record("k", parse_pr(pr(comments=[c(30, "sam", "New question")]))[1])
    assert "c:30" in store.pr("k")["pending"]


def test_the_store_survives_a_restart(tmp_path):
    path = tmp_path / "c.json"
    store = CommentStore(path)
    store.numbers["PP-1"] = 7
    store.record("k", parse_pr(pr(threads=[thread("T1", [c(1, "sam", "Rename")])]))[1])
    store.save()
    again = CommentStore(path)
    assert again.numbers == {"PP-1": 7} and keys(again.pending_items("k")) == ["t:1"]
    assert again.record("k", parse_pr(pr(threads=[thread("T1", [c(1, "sam", "Rename")])]))[1]) == []


def test_feedback_file_lists_every_item_with_ids_and_the_rules_pointer():
    facts, items = parse_pr(
        pr(
            threads=[thread("T1", [c(1, "sam", "Rename\nthis")])],
            comments=[c(14, "owner", "Also the empty case")],
        )
    )
    text = feedback_text("o/r", facts, items)
    assert (
        "PR #7 (o/r)" in text and "~/.claude/agents/spp-builder.md" in text and "comment.sh" in text
    )
    assert "`t:1`" in text and "Thread node id `T1`" in text and "> @sam: Rename" in text
    assert "Elliot (an instruction)" in text


def test_read_actions_keeps_the_last_record_per_item(tmp_path):
    (tmp_path / "spp-comments.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"id": "t:1", "action": "needs_you", "reply": "draft"},
                {"id": "t:1", "action": "fixed", "reply": "ok"},
            ]
        )
        + "\nnot json\n"
    )
    assert read_actions(tmp_path)["t:1"]["action"] == "fixed"
    assert read_actions(tmp_path / "missing") == {}


def test_lane_counts_and_your_decisions_override_the_builders_record(tmp_path):
    store = CommentStore(tmp_path / "c.json")
    items = parse_pr(
        pr(threads=[thread(f"T{n}", [c(n, "sam", f"c{n}")]) for n in (1, 2, 3, 4, 5)])
    )[1]
    store.record("k", items)
    store.mark_delivered("k")
    actions = {
        "t:1": {"action": "fixed", "reply": "Fixed"},
        "t:2": {"action": "replied", "reply": "Answered"},
        "t:3": {"action": "skipped", "reply": ""},
        "t:4": {"action": "needs_you", "reply": "Draft reply"},
    }
    view = comments_view(store.peek("k"), actions)
    assert (view["addressed"], view["needsYou"], view["waiting"]) == (3, 1, 1)
    assert view["items"][0]["state"] == "needs_you" and view["items"][0]["reply"] == "Draft reply"
    store.resolve("k", "t:4", "posted", "Edited reply")
    after = comments_view(store.peek("k"), actions)
    assert (after["addressed"], after["needsYou"]) == (4, 0)
    empty = comments_view(None, {})
    assert (
        empty["addressed"],
        empty["needsYou"],
        empty["waiting"],
        empty["items"],
        empty["lane"],
    ) == (0, 0, 0, [], "")


class Fake:
    def __init__(self):
        self.raw = pr()
        self.found = type("P", (), {"number": 7})()
        self.fetches = 0

    def fetch(self, repo, number):
        self.fetches += 1
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw

    def find(self, repo, branch):
        return self.found


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    manager = SessionManager(settings)
    session = add_session(manager, tmp_path, issue("ABC-12", team="ABC"))
    session.pty.sent = []
    session.pty.write = session.pty.sent.append
    monkeypatch.setattr(
        "cyclecontrol.sessions.threading.Timer",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    fake = Fake()
    clock = {"now": 1000.0}
    watcher = CommentWatcher(
        settings,
        manager,
        CommentStore(tmp_path / "c.json"),
        fake.fetch,
        fake.find,
        lambda: clock["now"],
        resolver=lambda _thread_id: None,
    )
    return watcher, manager, session, fake, clock, settings


def test_nothing_new_means_nobody_is_woken_and_nothing_is_sent(rig):
    watcher, _, session, fake, _, _ = rig
    assert watcher.poll() == [] and fake.fetches == 1
    assert watcher.tick() == [] and session.pty.sent == []


def test_new_feedback_waits_while_the_builder_is_busy_then_is_delivered_once_when_idle(rig):
    watcher, _, session, fake, _clock, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    assert watcher.poll() == ["ABC-12"]
    session.pty.last_output = time.time()
    assert watcher.tick() == [] and session.pty.sent == []
    assert len(watcher._store.pending_items("o/abc#7")) == 1
    session.pty.last_output = 0.0
    assert watcher.tick() == ["ABC-12"]
    message = session.pty.sent[0].decode()
    assert "PR #7" in message and "spp-builder.md" in message and "\n" not in message
    files = list((session.git_dir / "spp-feedback").glob("*.md"))
    assert len(files) == 1 and "Rename this" in files[0].read_text()
    assert watcher.tick() == [] and len(session.pty.sent) == 1


def test_a_merged_pr_is_kept_as_merged_and_not_fetched_again(rig):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(state="MERGED")
    watcher.poll()
    state = watcher._store.state_of("o/abc#7")
    assert state["open"] is False and state["merged"] is True
    fetches = fake.fetches
    watcher.poll()
    assert fake.fetches == fetches
    watcher._store.set_state("o/abc#7", {**state, "open": True})
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "x")])])
    session.stopped = True
    fetches = fake.fetches
    assert watcher.poll() == [] and fake.fetches == fetches


def test_a_github_error_skips_the_pr_quietly(rig):
    watcher, _, _, fake, _, _ = rig
    fake.raw = GithubError("boom")
    assert watcher.poll() == []


def test_an_ended_builder_is_resumed_then_gets_the_feedback_when_idle(rig, monkeypatch):
    watcher, _manager, session, fake, _clock, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    watcher.poll()
    session.pty.alive = False
    revived = []
    monkeypatch.setattr(
        SessionManager,
        "revive_for_comments",
        lambda self, i, mode, base, branch: revived.append((i.id, mode, base, branch)) or "started",
    )
    watcher.tick()
    watcher.tick()
    assert revived == [("ABC-12", "plan", "", "feature/abc-12")]


def test_when_resume_fails_a_new_builder_is_started_with_the_pr_number(rig, monkeypatch):
    watcher, _manager, session, fake, _clock, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    watcher.poll()
    session.pty.alive = False
    started = []

    def boom(self, i, mode, base, branch):
        raise RuntimeError("no conversation to resume")

    monkeypatch.setattr(SessionManager, "revive_for_comments", boom)
    monkeypatch.setattr(
        SessionManager,
        "start_fresh_for_pr",
        lambda self, i, mode, base, prompt: started.append(prompt) or "started",
    )
    watcher.tick()
    assert len(started) == 1 and "PR #7" in started[0] and "spp-feedback" in started[0]
    assert watcher._store.pending_items("o/abc#7") == []


def test_a_resumed_builder_that_dies_straight_away_falls_back_to_a_new_one(rig, monkeypatch):
    watcher, _manager, session, fake, clock, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    watcher.poll()
    session.pty.alive = False
    started = []

    def revive(self, i, mode, base, branch):
        newborn = add_session(self, session.git_dir.parent, i)
        newborn.started_at = clock["now"] + 1
        newborn.pty.alive = False
        return "started"

    monkeypatch.setattr(SessionManager, "revive_for_comments", revive)
    monkeypatch.setattr(
        SessionManager,
        "start_fresh_for_pr",
        lambda self, i, mode, base, prompt: started.append(prompt) or "started",
    )
    watcher.tick()
    assert started == []
    clock["now"] += 5
    watcher.tick()
    assert len(started) == 1 and "PR #7" in started[0]


def test_posting_an_approved_reply_marks_it_as_the_builders_and_records_your_decision(
    rig, monkeypatch
):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Drop the retry")])], comments=[])
    watcher.poll()
    posted = []
    monkeypatch.setattr(
        commentwatch,
        "reply_to_review_comment",
        lambda repo, n, cid, text: posted.append((repo, n, cid, text)),
    )
    watcher.post_reply(session, "t:1", "I would keep it.")
    assert posted == [("o/abc", 7, 1, f"I would keep it.\n\n{MARKER}")]
    assert watcher._store.peek("o/abc#7")["resolutions"]["t:1"]["state"] == "posted"
    with pytest.raises(GithubError):
        watcher.post_reply(session, "t:999", "x")


def test_bots_without_a_bot_suffix_are_recognised_by_their_github_type():
    bot = {
        "databaseId": 40,
        "author": {"login": "aviator-app", "__typename": "Bot"},
        "body": "# Current Aviator status",
        "url": "u",
        "createdAt": "",
    }
    codecov = {
        "databaseId": 41,
        "author": {"login": "street-codecov", "__typename": "Bot"},
        "body": "## Codecov report",
        "url": "u",
        "createdAt": "",
    }
    rabbit = {
        "databaseId": 42,
        "author": {"login": "coderabbitai", "__typename": "Bot"},
        "body": "Use a constant here",
        "url": "u",
        "createdAt": "",
    }
    human = {
        "databaseId": 43,
        "author": {"login": "rio", "__typename": "User"},
        "body": "Please rename",
        "url": "u",
        "createdAt": "",
    }
    _, items = parse_pr(pr(comments=[bot, codecov, rabbit, human]))
    assert keys(items) == ["c:43"]
    _, threads = parse_pr(pr(threads=[thread("T9", [codecov]), thread("T10", [rabbit])]))
    assert keys(threads) == ["t:42"]


def test_posting_a_reply_marks_the_item_and_the_new_comment_seen_so_it_never_returns(
    rig, monkeypatch
):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Drop the retry")])])
    watcher.poll()
    store = watcher._store
    assert store.peek("o/abc#7")["pending"] == ["t:1"]
    posted = []
    monkeypatch.setattr(
        commentwatch,
        "reply_to_review_comment",
        lambda repo, n, cid, text: posted.append(text) or 555,
    )
    watcher.post_reply(session, "t:1", "I would keep it.")
    state = store.peek("o/abc#7")
    assert posted == [f"I would keep it.\n\n{MARKER}"]
    assert {"t:1", "t:555"} <= set(state["seen"]) and state["pending"] == []
    assert state["resolutions"]["t:1"]["state"] == "posted"
    session.pty.last_output = 0.0
    assert watcher.tick() == [] and session.pty.sent == []
    fake.raw = pr(
        threads=[
            thread(
                "T1",
                [c(1, "sam", "Drop the retry"), c(555, "owner", f"I would keep it.\n\n{MARKER}")],
            )
        ]
    )
    assert watcher.poll() == []
    fake.raw = pr(
        threads=[
            thread(
                "T1",
                [
                    c(1, "sam", "Drop the retry"),
                    c(555, "owner", f"I would keep it.\n\n{MARKER}"),
                    c(556, "sam", "Still no"),
                ],
            )
        ]
    )
    assert watcher.poll() == ["ABC-12"]


def test_a_posted_pr_comment_is_seen_even_if_it_came_back_without_its_marker(rig, monkeypatch):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(comments=[c(3, "sam", "Is it in the spec?")])
    watcher.poll()
    monkeypatch.setattr(commentwatch, "comment_on_pr", lambda repo, n, text: 777)
    watcher.post_reply(session, "c:3", "Yes, section 2.")
    assert {"c:3", "c:777"} <= set(watcher._store.peek("o/abc#7")["seen"])
    fake.raw = pr(comments=[c(3, "sam", "Is it in the spec?"), c(777, "owner", "Yes, section 2.")])
    assert watcher.poll() == []


def test_dismissing_marks_the_item_seen_and_not_pending(rig):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Drop the retry")])])
    watcher.poll()
    watcher.dismiss(session, "t:1")
    state = watcher._store.peek("o/abc#7")
    assert (
        state["pending"] == []
        and "t:1" in state["seen"]
        and state["resolutions"]["t:1"]["state"] == "dismissed"
    )


def test_the_gh_reply_helpers_return_the_new_comment_id(monkeypatch):
    import subprocess

    from cyclecontrol import github

    def fake_run(argv, **kwargs):
        fake_run.calls.append((argv, kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, '{"id": 321}', "")

    fake_run.calls = []
    monkeypatch.setattr(github.subprocess, "run", fake_run)
    assert github.reply_to_review_comment("o/r", 7, 5, "hi") == 321
    assert github.comment_on_pr("o/r", 7, "hi") == 321
    assert (
        "pulls/7/comments/5/replies" in fake_run.calls[0][0][2]
        and "issues/7/comments" in fake_run.calls[1][0][2]
    )
    monkeypatch.setattr(
        github.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, "not json", ""),
    )
    assert github.comment_on_pr("o/r", 7, "hi") == 0
    monkeypatch.setattr(
        github.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "boom")
    )
    with pytest.raises(GithubError):
        github.comment_on_pr("o/r", 7, "hi")


def test_you_are_told_once_when_a_pr_becomes_ready_to_merge(rig):
    from cyclecontrol.comments import pr_state
    from tests.test_approval import full_pr

    watcher, _, _session, _fake, _, _ = rig
    notes = []
    watcher._notify = lambda title, message: notes.append(title)
    watcher.poll()
    store = watcher._store
    store.set_state("o/abc#7", pr_state(full_pr()))
    watcher.tick()
    watcher.tick()
    assert notes == ["ABC-12 is ready to merge"]
    store.set_state("o/abc#7", pr_state(full_pr(labels={"nodes": [{"name": "blocked"}]})))
    watcher.tick()
    store.set_state("o/abc#7", pr_state(full_pr()))
    watcher.tick()
    assert notes == ["ABC-12 is ready to merge", "ABC-12 is ready to merge"]
    notes.clear()
    queued = pr_state(full_pr(labels={"nodes": [{"name": "mergequeue"}]}))
    store.set_state("o/abc#7", queued)
    watcher.tick()
    assert notes == []


def test_a_refused_requeue_notifies_once_per_attempt_and_a_requeue_does_not(rig):
    watcher, _, session, _fake, _, _ = rig
    notes = []
    watcher._notify = lambda title, message: notes.append((title, message))
    watcher.poll()
    note = session.git_dir / "spp-requeue.json"
    note.write_text(
        json.dumps(
            {
                "status": "refused",
                "reasons": ["CI is failure", "1 thread(s) are unresolved"],
                "at": "t1",
            }
        )
    )
    watcher.tick()
    watcher.tick()
    assert notes == [("ABC-12 was not re-queued", "CI is failure; 1 thread(s) are unresolved")]
    note.write_text(json.dumps({"status": "refused", "reasons": ["CI is failure"], "at": "t2"}))
    watcher.tick()
    assert len(notes) == 2
    note.write_text(json.dumps({"status": "requeued", "reasons": [], "at": "t3"}))
    watcher.tick()
    assert len(notes) == 2


def test_the_view_says_when_comments_were_last_checked_what_was_new_and_when_it_was_sent(rig):
    watcher, _, session, fake, clock, _ = rig
    assert watcher.view(session)["checkedAt"] == 0 and watcher.view(session)["hasPr"] is False
    watcher.poll()
    view = watcher.view(session)
    assert (
        view["hasPr"],
        view["prNumber"],
        view["checkedAt"],
        view["lastNew"],
        view["deliveredAt"],
    ) == (True, 7, 1000.0, 0, 0)
    assert view["intervalSeconds"] == 300 and view["enabled"] is True
    clock["now"] = 1100.0
    fake.raw = pr(
        threads=[thread("T1", [c(1, "sam", "Rename")]), thread("T2", [c(2, "sam", "Add test")])]
    )
    watcher.poll()
    view = watcher.view(session)
    assert (view["checkedAt"], view["lastNew"], view["deliveredAt"]) == (1100.0, 2, 0)
    session.pty.last_output = 0.0
    clock["now"] = 1105.0
    watcher.tick()
    assert watcher.view(session)["deliveredAt"] == 1105.0
    assert watcher.last_poll == 1100.0


DRAFT = "Draft reply (not posted): Kept on purpose. <!-- spp-builder -->"


def pending_thread(rig):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(threads=[thread("PRRT_1", [c(1, "sam", "Drop the retry")])])
    watcher.poll()
    return watcher, session


def test_posting_a_draft_never_sends_its_draft_label_and_adds_exactly_one_marker(rig, monkeypatch):
    watcher, session = pending_thread(rig)
    posted = []
    monkeypatch.setattr(
        commentwatch,
        "reply_to_review_comment",
        lambda repo, n, cid, text: posted.append(text) or 555,
    )
    watcher.post_reply(session, "t:1", DRAFT)
    assert posted == [f"Kept on purpose.\n\n{MARKER}"]
    assert posted[0].count(MARKER) == 1 and "Draft" not in posted[0]
    resolution = watcher._store.peek("o/abc#7")["resolutions"]["t:1"]
    assert resolution["reply"] == "Kept on purpose."
    assert resolution["posted_url"] == "https://github.com/o/abc/pull/7#discussion_r555"
    assert resolution["resolved"] is False


def test_an_empty_reply_after_cleaning_is_refused(rig, monkeypatch):
    watcher, session = pending_thread(rig)
    monkeypatch.setattr(
        commentwatch, "reply_to_review_comment", lambda *a: pytest.fail("must not post")
    )
    with pytest.raises(GithubError, match="empty"):
        watcher.post_reply(session, "t:1", "Draft reply (not posted): <!-- spp-builder -->")


def test_post_and_resolve_resolves_the_thread_and_post_alone_leaves_it_open(rig, monkeypatch):
    watcher, session = pending_thread(rig)
    resolved = []
    monkeypatch.setattr(commentwatch, "reply_to_review_comment", lambda *a: 555)
    monkeypatch.setattr(
        commentwatch, "resolve_thread", lambda thread_id: resolved.append(thread_id)
    )
    watcher.post_reply(session, "t:1", "Kept.")
    assert resolved == []
    watcher.post_reply(session, "t:1", "Kept.", resolve=True)
    assert resolved == ["PRRT_1"]
    assert watcher._store.peek("o/abc#7")["resolutions"]["t:1"]["resolved"] is True


def test_if_resolving_fails_the_reply_stays_posted_and_you_are_told(rig, monkeypatch):
    watcher, session = pending_thread(rig)
    monkeypatch.setattr(commentwatch, "reply_to_review_comment", lambda *a: 555)

    def boom(thread_id):
        raise GithubError("no permission")

    monkeypatch.setattr(commentwatch, "resolve_thread", boom)
    with pytest.raises(GithubError, match="reply is posted, but the thread could not be resolved"):
        watcher.post_reply(session, "t:1", "Kept.", resolve=True)
    resolution = watcher._store.peek("o/abc#7")["resolutions"]["t:1"]
    assert resolution["state"] == "posted" and resolution["resolved"] is False


def test_resolving_is_ignored_for_comments_that_are_not_threads(rig, monkeypatch):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(comments=[c(3, "sam", "Is it in the spec?")])
    watcher.poll()
    monkeypatch.setattr(commentwatch, "comment_on_pr", lambda *a: 777)
    monkeypatch.setattr(commentwatch, "resolve_thread", lambda t: pytest.fail("not a thread"))
    watcher.post_reply(session, "c:3", "Yes.", resolve=True)
    assert watcher._store.peek("o/abc#7")["resolutions"]["c:3"]["posted_url"].endswith(
        "#issuecomment-777"
    )


def test_the_view_shows_clean_replies_links_and_who_is_being_picked_up(rig):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(
        threads=[thread("T1", [c(1, "sam", "Rename")]), thread("T2", [c(2, "lee", "Add test")])]
    )
    watcher.poll()
    session.pty.last_output = 0.0
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps({"id": "t:1", "action": "needs_you", "reply": DRAFT}) + "\n"
    )
    watcher.tick()
    view = watcher.view(session)
    by_key = {row["key"]: row for row in view["items"]}
    assert by_key["t:1"]["reply"] == "Kept on purpose." and by_key["t:1"]["url"] == "https://x/1"
    assert by_key["t:2"]["state"] == "delivered" and view["authors"] == ["lee"]
    assert view["repoUrl"] == "https://github.com/o/abc"


def test_you_are_told_when_the_builder_picks_up_comments_and_once_per_item_that_needs_you(rig):
    watcher, _, session, fake, _, _ = rig
    notes = []
    watcher._notify = lambda title, message: notes.append((title, message))
    watcher.poll()
    fake.raw = pr(
        threads=[thread("T1", [c(1, "sam", "Rename")]), thread("T2", [c(2, "lee", "Add test")])]
    )
    watcher.poll()
    session.pty.last_output = 0.0
    watcher.tick()
    assert notes == [("ABC-12 is picking up 2 review comments", "From @sam, @lee")]
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps({"id": "t:1", "action": "needs_you", "reply": "x"}) + "\n"
    )
    watcher.tick()
    watcher.tick()
    assert [t for t, _ in notes].count("ABC-12: a review comment needs you") == 1
    assert notes[-1][1].startswith("@sam: ")


def test_the_feedback_file_lists_what_is_already_documented_before_the_builder_escalates(tmp_path):
    from cyclecontrol.comments import plan_context

    (tmp_path / "spp-plan.json").write_text(
        json.dumps(
            {
                "summary": "PR 1 lands date-fns and a ported date.ts",
                "decisions": ["Keep the dayjs wording for relativeDate (Elliot, plan approval)"],
                "risks": [
                    "relativeDate wording: dayjs says 'an hour ago'",
                    *[f"risk {n}" for n in range(20)],
                ],
            }
        )
    )
    text = plan_context(tmp_path, "https://linear.app/x/issue/PP-1035")
    assert "The ticket: https://linear.app/x/issue/PP-1035" in text
    assert "Decision: Keep the dayjs wording for relativeDate" in text
    assert "Risk noted in the plan: relativeDate wording" in text and text.count("Risk noted") == 8
    assert "reply with it yourself" in text
    bare = plan_context(tmp_path / "missing")
    assert "ticket" not in bare.lower().split("the pr description")[0] and "Decision:" not in bare


def test_feedback_text_carries_the_context_and_delivery_writes_it(rig):
    watcher, _, session, fake, _, _ = rig
    (session.git_dir / "spp-plan.json").write_text(json.dumps({"decisions": ["Keep the wording"]}))
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Why keep the old wording?")])])
    watcher.poll()
    session.pty.last_output = 0.0
    watcher.tick()
    written = next((session.git_dir / "spp-feedback").glob("*.md")).read_text()
    assert (
        "Already documented (check before you escalate)" in written
        and "Decision: Keep the wording" in written
    )
    assert session.issue.url in written


def test_posting_a_draft_as_it_is_is_recorded_as_unchanged_and_an_edit_is_not(rig, monkeypatch):
    watcher, _, session, fake, _, _ = rig
    watcher.poll()
    fake.raw = pr(
        threads=[thread("T1", [c(1, "sam", "Drop it")]), thread("T2", [c(2, "sam", "Rename it")])]
    )
    watcher.poll()
    (session.git_dir / "spp-comments.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "id": k,
                    "action": "needs_you",
                    "reply": "Draft reply (not posted): Kept on purpose.",
                }
            )
            for k in ("t:1", "t:2")
        )
        + "\n"
    )
    monkeypatch.setattr(commentwatch, "reply_to_review_comment", lambda *a: 555)
    watcher.post_reply(session, "t:1", "Draft reply (not posted): Kept on purpose.")
    watcher.post_reply(session, "t:2", "Kept on purpose, and here is why.")
    resolutions = watcher._store.peek("o/abc#7")["resolutions"]
    assert resolutions["t:1"]["unchanged"] is True and resolutions["t:2"]["unchanged"] is False


def blocked_ready_pr(head="H1", **over):
    from tests.test_approval import full_pr

    data = full_pr(headRefOid=head, **{"labels": {"nodes": [{"name": "blocked"}]}, **over})
    data["number"] = 7
    return data


@pytest.fixture
def requeue_rig(rig):
    watcher, _manager, session, fake, _clock, _settings = rig
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs.get("cwd")))
        if runner.note:
            (session.git_dir / "spp-requeue.json").write_text(json.dumps(runner.note))
        import subprocess

        return subprocess.CompletedProcess(argv, runner.code, "", "boom")

    runner.note = {"status": "requeued", "reasons": [], "at": "t1", "pr": 7}
    runner.code = 0
    watcher._run = runner
    notes = []
    watcher._notify = lambda title, message: notes.append(title)
    fake.raw = blocked_ready_pr()
    watcher.poll()
    session.pty.last_output = 0.0
    return watcher, session, fake, calls, notes, runner


def test_a_blocked_but_ready_pr_is_requeued_once_per_commit_through_requeue_sh(requeue_rig):
    watcher, session, fake, calls, notes, _runner = requeue_rig
    watcher.tick()
    watcher.tick()
    assert len(calls) == 1
    argv, cwd = calls[0]
    assert argv[0] == "bash" and argv[1].endswith("/.claude/spp/requeue.sh") and argv[2] == "7"
    assert cwd == str(session.worktree)
    labels = watcher._store.state_of("o/abc#7")["labels"]
    assert "mergequeue" in labels and "blocked" not in labels
    assert "ABC-12 was re-queued" in notes
    assert watcher.view(session)["lane"] == "re-queued"
    fake.raw = blocked_ready_pr(head="H2")
    watcher.poll()
    watcher.tick()
    assert len(calls) == 2


def test_nothing_is_attempted_while_the_builder_is_working(requeue_rig):
    watcher, session, _fake, calls, _notes, _runner = requeue_rig
    session.pty.last_output = time.time()
    watcher.tick()
    assert calls == []
    session.pty.last_output = 0.0
    watcher.tick()
    assert len(calls) == 1


def test_an_ended_builders_worktree_is_still_requeued(requeue_rig, monkeypatch):
    watcher, session, _fake, calls, _notes, _runner = requeue_rig
    session.pty.alive = False
    monkeypatch.setattr(SessionManager, "worktree_free", lambda self, s: (False, "held"))
    watcher.tick()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"reviews": {"nodes": []}},
        {"reviewThreads": {"nodes": [thread("T1", [c(1, "rio", "still open")])]}},
        {
            "commits": {
                "nodes": [{"commit": {"oid": "H1", "statusCheckRollup": {"state": "FAILURE"}}}]
            }
        },
        {"labels": {"nodes": []}},
        {"state": "CLOSED"},
    ],
)
def test_a_pr_that_does_not_qualify_never_reaches_the_script(requeue_rig, change):
    watcher, _session, fake, calls, _notes, _runner = requeue_rig
    fake.raw = blocked_ready_pr(**change)
    watcher.poll()
    watcher.tick()
    assert calls == []


def test_a_needs_you_item_holds_the_requeue_back(requeue_rig):
    watcher, session, _fake, calls, _notes, _runner = requeue_rig
    watcher._store.record(
        "o/abc#7", parse_pr(pr(threads=[thread("T1", [c(1, "sam", "Drop it")])]))[1]
    )
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps({"id": "t:1", "action": "needs_you", "reply": "x"}) + "\n"
    )
    watcher.tick()
    assert calls == []


def test_when_the_script_refuses_the_lane_says_so_and_you_are_told_once(requeue_rig):
    watcher, _session, _fake, _calls, notes, runner = requeue_rig
    runner.code = 1
    runner.note = {
        "status": "refused",
        "reasons": ["mergequeue was last applied by sam, not by you"],
        "at": "t9",
        "pr": 7,
    }
    watcher.tick()
    watcher.tick()
    assert [n for n in notes if "not re-queued" in n] == ["ABC-12 was not re-queued"]
    labels = watcher._store.state_of("o/abc#7")["labels"]
    assert "blocked" in labels and "mergequeue" not in labels
    assert "was re-queued" not in " ".join(notes)


def test_requeue_can_be_turned_off(requeue_rig):
    watcher, _session, _fake, calls, _notes, _runner = requeue_rig
    watcher._settings.auto_requeue = False
    watcher.tick()
    assert calls == []


def test_a_state_saved_by_an_older_version_does_not_stop_the_local_step(requeue_rig):
    watcher, _session, _fake, calls, _notes, _runner = requeue_rig
    old = dict(watcher._store.state_of("o/abc#7"))
    old.pop("head")
    watcher._store.set_state("o/abc#7", old)
    watcher.tick()
    assert len(calls) == 1, "still attempts the re-queue, keyed on an empty head"


def test_a_queued_pr_is_checked_every_minute_and_others_every_five(rig):
    watcher, _, _, fake, _, _ = rig
    watcher.poll()
    fetched = fake.fetches
    assert watcher.poll(only_due=True) == [] and fake.fetches == fetched
    state = watcher._store.state_of("o/abc#7")
    watcher._store.set_state("o/abc#7", {**state, "labels": ["mergequeue"]})
    watcher._store.pr("o/abc#7")["checked_at"] = watcher._clock() - 61
    watcher.poll(only_due=True)
    assert fake.fetches == fetched + 1


def test_the_default_lookup_finds_a_merged_pr(monkeypatch):
    from cyclecontrol import commentwatch
    from cyclecontrol.github import PrState

    merged = PrState(7, "https://x/pull/7", "feature/x", "MERGED", "develop", "abc")
    monkeypatch.setattr(commentwatch, "find_pr_any_state", lambda repo, branch: merged)
    assert commentwatch.find_latest_pr("o/r", "feature/x").number == 7


def test_hidden_html_comments_are_not_shown_in_a_review_body():
    from cyclecontrol.comments import visible

    assert visible("<!-- coderabbit-ai-generated:v1 -->\nUse a constant.") == "Use a constant."
    assert visible("a <!-- x\ny --> b") == "a  b"


def test_a_thread_resolved_on_github_is_marked_on_its_card(rig):
    watcher, _, _, fake, _, _ = rig
    raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    fake.raw = raw
    watcher.poll()
    card = comments_view(watcher._store.peek("o/abc#7"), {})["items"][0]
    assert card["githubResolved"] is False
    raw["reviewThreads"]["nodes"][0]["isResolved"] = True
    watcher.poll()
    card = comments_view(watcher._store.peek("o/abc#7"), {})["items"][0]
    assert card["githubResolved"] is True


def test_an_approval_with_a_real_comment_is_feedback_but_plain_praise_is_not():
    raw = pr(
        reviews=[
            review(
                30, "sam", "APPROVED", "Update a skill to use the existing EmptyState component."
            ),
            review(31, "lee", "APPROVED", "LGTM!"),
            review(32, "kim", "APPROVED", ""),
        ]
    )
    _, items = parse_pr(raw)
    assert keys(items) == ["r:30"]


def _answered_thread(tid, opener, resolved=False, builder_last=True):
    comments = [c(1, opener, "please change")]
    if builder_last:
        comments.append(c(2, "owner", f"Agreed, deferring. {MARKER}"))
    return thread(tid, comments, resolved=resolved)


def test_a_thread_the_builder_answered_last_is_resolved_unless_the_opener_requested_changes(rig):
    from cyclecontrol.comments import pr_state

    watcher, _, _, _, _, _ = rig
    done = []
    watcher._resolver = done.append
    raw = pr(
        threads=[
            _answered_thread("A", "sam"),
            _answered_thread("B", "lee"),
            _answered_thread("C", "kim", builder_last=False),
            _answered_thread("D", "sam", resolved=True),
        ],
        reviews=[review(5, "lee", "CHANGES_REQUESTED", "x")],
    )
    watcher._resolve_answered_threads("o/abc#7", raw, pr_state(raw))
    assert done == ["A"]
    assert raw["reviewThreads"]["nodes"][0]["isResolved"] is True


def test_a_reply_posted_without_resolving_is_left_open(rig):
    from cyclecontrol.comments import pr_state

    watcher, _, _, _, _, _ = rig
    done = []
    watcher._resolver = done.append
    raw = pr(threads=[_answered_thread("A", "sam")])
    store = watcher._store
    store.pr("o/abc#7")["items"]["t:1"] = {"thread_id": "A"}
    store.resolve("o/abc#7", "t:1", "posted", "x", resolved=False)
    watcher._resolve_answered_threads("o/abc#7", raw, pr_state(raw))
    assert done == []


def test_a_builder_whose_pr_is_merged_is_finished_and_not_revived(rig, monkeypatch):
    watcher, manager, session, fake, _clock, settings = rig
    old_pty = session.pty
    fake.raw = pr(state="MERGED")
    watcher.poll()
    revived = []
    monkeypatch.setattr(
        SessionManager, "revive_for_comments", lambda self, *a: revived.append(1) or "started"
    )
    watcher.tick()
    assert session.finished and old_pty.terminated and not session.pty.is_alive()
    assert session.outcome(20) == "finished" and manager.live() == []
    watcher._store.pr("o/abc#7")["pending"] = ["t:1"]
    watcher.tick()
    watcher.tick()
    assert revived == []
    saved = {e["issue"]["id"]: e for e in read_registry(settings.state_path)["sessions"]}
    assert saved["ABC-12"]["finished"] is True


def test_a_sleeping_builder_is_only_revived_for_new_feedback(rig, monkeypatch):
    watcher, manager, session, _fake, _clock, _ = rig
    watcher.poll()
    manager._sleep(session)
    revived = []
    monkeypatch.setattr(
        SessionManager, "revive_for_comments", lambda self, *a: revived.append(1) or "started"
    )
    monkeypatch.setattr("cyclecontrol.locks.PROCS.claude_pids_in", lambda path: [])
    watcher.tick()
    assert revived == [] and session.outcome(20) == "sleeping"
    _fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    assert watcher.poll() == ["ABC-12"]
    watcher.tick()
    assert revived == [1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Waiting on Elliot to pick: (1) rerun the suite (2) skip the browser job (3) revert",
            ["rerun the suite", "skip the browser job", "revert"],
        ),
        ("Pick one:\n1) Rerun it\n2) Skip it\n", ["Rerun it", "Skip it"]),
        ("1. Retry\n2. Investigate the flake\n", ["Retry", "Investigate the flake"]),
        ("(1) a\n(2) b", ["a", "b"]),
        ("CI is red and I am not sure why.", []),
    ],
)
def test_numbered_options_are_parsed_out_of_the_builders_note(text, expected):
    from cyclecontrol.comments import builder_options

    found = builder_options(text)
    assert [o["label"] for o in found] == expected
    assert [o["key"] for o in found] == [str(n) for n in range(1, len(expected) + 1)]


def ci_rig(rig, monkeypatch, phase="human"):
    from tests.test_approval import full_pr

    watcher, manager, session, fake, _clock, _settings = rig
    (session.git_dir / "spp-phase.json").write_text(json.dumps({"phase": phase}))
    fake.raw = full_pr(
        headRefOid="ef3763b7aa",
        commits={
            "nodes": [
                {
                    "commit": {
                        "oid": "ef3763b7aa",
                        "statusCheckRollup": {
                            "state": "FAILURE",
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "ci/circleci: test-php-browser",
                                        "status": "COMPLETED",
                                        "conclusion": "FAILURE",
                                    }
                                ]
                            },
                        },
                    }
                }
            ]
        },
    )
    for name in ("reply_to_review_comment", "comment_on_pr", "resolve_thread"):
        monkeypatch.setattr(
            commentwatch,
            name,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("GitHub touched")),
        )
    assert watcher.poll() == ["ABC-12"]
    watcher._store.mark_delivered("o/abc#7")
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps(
            {
                "id": "ci:ef3763b7",
                "action": "needs_you",
                "reply": "Waiting on Elliot to pick: (1) rerun the suite (2) skip the browser job",
            }
        )
        + "\n"
    )
    return watcher, manager, session, fake


def test_a_ci_item_that_needs_you_shows_the_builders_options_and_its_kind(rig, monkeypatch):
    watcher, _manager, session, _fake = ci_rig(rig, monkeypatch)
    view = watcher.view(session)
    item = view["items"][0]
    assert (item["kind"], item["state"], view["needsYou"]) == ("ci", "needs_you", 1)
    assert [o["label"] for o in item["options"]] == ["rerun the suite", "skip the browser job"]
    assert item["reply"].startswith("Waiting on Elliot")


def test_answering_a_ci_item_goes_to_the_builder_not_github_and_is_not_raised_again(
    rig, monkeypatch
):
    watcher, _manager, session, fake = ci_rig(rig, monkeypatch)
    assert (
        watcher.answer_builder(session, "ci:ef3763b7", "Go with option 1: rerun the suite")
        == "running"
    )
    assert session.pty.sent == [b"Go with option 1: rerun the suite"]
    view = watcher.view(session)
    assert view["needsYou"] == 0 and view["items"][0]["state"] == "answered"
    assert view["addressed"] == 1 and watcher._store.peek("o/abc#7")["pending"] == []
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    assert watcher.poll() == [] and watcher._store.peek("o/abc#7")["pending"] == []
    assert watcher.view(session)["items"][0]["state"] == "answered"
    fake.raw["headRefOid"] = "ff00ff00aa"
    fake.raw["commits"]["nodes"][0]["commit"]["oid"] = "ff00ff00aa"
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    assert watcher.poll() == ["ABC-12"]
    assert [k for k in watcher._store.peek("o/abc#7")["items"]] == ["ci:ff00ff00"]


def test_dismissing_a_ci_item_ignores_that_commit_only(rig, monkeypatch):
    watcher, _manager, session, _fake = ci_rig(rig, monkeypatch)
    watcher.dismiss(session, "ci:ef3763b7")
    assert watcher.view(session)["needsYou"] == 0 and session.pty.sent == []
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    assert watcher.poll() == []


def test_answering_a_sleeping_builder_wakes_it_with_the_answer(rig, monkeypatch):
    watcher, manager, session, _fake = ci_rig(rig, monkeypatch)
    manager._sleep(session)
    woken = []
    monkeypatch.setattr(
        SessionManager, "wake", lambda self, i, prompt="": woken.append((i, prompt)) or "started"
    )
    assert watcher.answer_builder(session, "ci:ef3763b7", "skip it") == "started"
    assert woken == [("ABC-12", "skip it")]


def test_a_review_thread_cannot_be_answered_to_the_builder_and_an_empty_answer_is_refused(
    rig, monkeypatch
):
    watcher, _manager, session, _fake = ci_rig(rig, monkeypatch)
    with pytest.raises(GithubError, match="empty"):
        watcher.answer_builder(session, "ci:ef3763b7", "  ")
    watcher._store.pr("o/abc#7")["items"]["t:9"] = {"kind": "thread", "author": "sam", "body": "x"}
    with pytest.raises(GithubError, match="Only a CI or conflict"):
        watcher.answer_builder(session, "t:9", "hi")


def conflict_rig(rig, monkeypatch, head="ef3763b7aa", mergeable="CONFLICTING", comments=()):
    from tests.test_approval import full_pr

    watcher, manager, session, fake, _clock, _settings = rig
    (session.git_dir / "spp-phase.json").write_text(json.dumps({"phase": "human"}))
    fake.raw = full_pr(
        headRefOid=head,
        mergeable=mergeable,
        baseRefName="develop",
        comments={"nodes": list(comments)},
    )
    watcher._store.numbers["ABC-12"] = 7
    woken = []
    monkeypatch.setattr(
        SessionManager, "wake", lambda self, i, prompt="": woken.append((i, prompt)) or "running"
    )
    for name in ("reply_to_review_comment", "comment_on_pr", "resolve_thread"):
        monkeypatch.setattr(
            commentwatch,
            name,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("GitHub touched")),
        )
    return watcher, manager, session, fake, woken


def test_a_conflict_is_handed_to_the_builder_once_per_sha_and_never_needs_you(rig, monkeypatch):
    watcher, _manager, session, fake, woken = conflict_rig(rig, monkeypatch)
    assert watcher.poll() == []
    assert len(woken) == 1 and woken[0][0] == "ABC-12"
    prompt = woken[0][1]
    assert "PR #7 has merge conflicts with develop" in prompt and "Do not ask Elliot" in prompt
    assert "requeue.sh" in prompt
    view = watcher.view(session)
    assert view["needsYou"] == 0 and view["waiting"] == 1
    assert view["items"][0]["kind"] == "conflict" and view["items"][0]["state"] == "delivered"
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    watcher.poll()
    watcher.tick()
    assert len(woken) == 1
    fake.raw["headRefOid"] = "ff00ff00aa"
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    watcher.poll()
    assert len(woken) == 1
    rig[4]["now"] += 601
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    watcher.poll()
    assert len(woken) == 2 and [k for k in watcher._store.peek("o/abc#7")["items"]] == [
        "conflict:ff00ff00"
    ]


def test_an_aviator_blocked_comment_counts_as_a_conflict(rig, monkeypatch):
    from cyclecontrol.comments import pr_state

    bot = c(99, "aviator-app[bot]", "This PR has merge conflicts and was removed from the queue.")
    watcher, _manager, _session, fake, woken = conflict_rig(
        rig, monkeypatch, mergeable="UNKNOWN", comments=[bot]
    )
    fake.raw["labels"] = {"nodes": [{"name": "blocked"}]}
    assert pr_state(fake.raw)["conflicting"] is True
    watcher.poll()
    assert len(woken) == 1
    fake.raw["labels"] = {"nodes": []}
    assert pr_state(fake.raw)["conflicting"] is False


def test_an_old_style_conflict_needs_you_is_dispatched_and_answered_not_shown(rig, monkeypatch):
    watcher, _manager, session, _fake, woken = conflict_rig(rig, monkeypatch)
    watcher._store.record(
        "o/abc#7",
        [
            ReviewItem(
                "conflict:ef3763b7", "conflict", "GitHub", False, False, "old body", url="https://x"
            )
        ],
    )
    watcher._store.mark_delivered("o/abc#7")
    watcher._store.set_state(
        "o/abc#7", {"open": True, "head": "ef3763b7aa", "base": "develop", "conflicting": True}
    )
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps(
            {
                "id": "conflict:ef3763b7",
                "action": "needs_you",
                "reply": "Say if I should rebase and resolve it.",
                "at": "2026-10-19T10:00:00Z",
            }
        )
        + "\n"
    )
    assert watcher.view(session)["needsYou"] == 1
    watcher.tick()
    assert len(woken) == 1 and "Resolve them yourself now" in woken[0][1]
    view = watcher.view(session)
    assert view["needsYou"] == 0 and view["items"][0]["state"] == "answered"
    watcher.tick()
    watcher.poll()
    assert len(woken) == 1


def test_a_contradiction_the_builder_raises_after_dispatch_stays_visible(rig, monkeypatch):
    from datetime import UTC, datetime, timedelta

    watcher, _manager, session, _fake, woken = conflict_rig(rig, monkeypatch)
    watcher.poll()
    assert len(woken) == 1
    later = ((datetime.now(UTC) + timedelta(minutes=1)).replace(microsecond=0).isoformat()).replace(
        "+00:00", "Z"
    )
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps(
            {
                "id": "conflict:ef3763b7",
                "action": "needs_you",
                "reply": "Two intents contradict: (1) keep the new column (2) drop it",
                "at": "2026-10-19T10:00:00Z",
            }
        )
        + "\n"
    )
    watcher.tick()
    view = watcher.view(session)
    assert view["needsYou"] == 1 and len(woken) == 1
    assert [o["label"] for o in view["items"][0]["options"]] == ["keep the new column", "drop it"]
    watcher.answer_builder(session, "conflict:ef3763b7", "Go with option 1: keep the new column")
    assert watcher.view(session)["needsYou"] == 0
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps(
            {"id": "conflict:ef3763b7", "action": "needs_you", "reply": "Still unsure", "at": later}
        )
        + "\n"
    )
    assert watcher.view(session)["needsYou"] == 1


def test_a_fixed_conflict_on_a_still_blocked_pr_shows_a_note_not_a_needs_you(rig, monkeypatch):
    watcher, _manager, session, fake, _woken = conflict_rig(rig, monkeypatch, mergeable="MERGEABLE")
    fake.raw["labels"] = {"nodes": [{"name": "blocked"}]}
    watcher._store.record(
        "o/abc#7",
        [ReviewItem("conflict:ef3763b7", "conflict", "GitHub", False, False, "b", url="https://x")],
    )
    watcher._store.mark_delivered("o/abc#7")
    watcher.poll()
    (session.git_dir / "spp-comments.jsonl").write_text(
        json.dumps({"id": "conflict:ef3763b7", "action": "fixed", "reply": "merged develop in"})
        + "\n"
    )
    view = watcher.view(session)
    assert view["needsYou"] == 0 and view["blockedNote"] == "blocked: not re-queued"
    fake.raw["labels"] = {"nodes": [{"name": "mergequeue"}]}
    watcher._store.pr("o/abc#7")["checked_at"] = 0
    watcher.poll()
    assert watcher.view(session)["blockedNote"] == ""


def test_a_pr_in_a_sibling_repo_is_found_by_issue_title_and_polled_there(rig):
    watcher, _manager, session, fake, _clock, _settings = rig
    fetched = []
    watcher._fetch = lambda repo, number: fetched.append((repo, number)) or fake.raw
    watcher._find_pr = lambda repo, branch: (_ for _ in ()).throw(GithubError("none"))
    watcher._find_by_issue = lambda owner, issue_id: ("Org/sibling", 99)
    watcher.poll()
    assert fetched == [("Org/sibling", 99)]
    assert watcher._store.repos[session.issue.id] == "Org/sibling"
    assert watcher._team_of(session).github_repo == "Org/sibling"


def test_an_approval_that_only_points_at_its_inline_comments_asks_for_nothing():
    raw = pr(
        reviews=[
            review(
                40, "sam", "APPROVED", "Approve-leaning. Nice consolidation. A few nits inline."
            ),
            review(41, "lee", "APPROVED", "Two small non-blocking cleanups inline."),
            review(
                42,
                "kim",
                "APPROVED",
                "Approving, but please regenerate the schema dump before merge.",
            ),
            review(43, "rio", "COMMENTED", "Nits inline."),
        ]
    )
    _, items = parse_pr(raw)
    assert keys(items) == ["r:42", "r:43"]


def test_the_first_look_delivers_a_review_body_that_is_less_than_a_day_old(tmp_path):
    from datetime import UTC, datetime, timedelta

    store = CommentStore(tmp_path / "c.json")
    now = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    fresh_review = review(50, "rio", "COMMENTED", "Not approving until the backfill is reworked")
    fresh_review["submittedAt"] = (now - timedelta(minutes=40)).isoformat().replace("+00:00", "Z")
    old = c(51, "sam", "old question", created=(now - timedelta(days=3)).isoformat())
    _, items = parse_pr(pr(reviews=[fresh_review], comments=[old]))
    assert keys(store.record("o/r#7", items, now.timestamp())) == ["r:50"]


def test_the_feedback_file_never_names_ci_or_github_as_a_reviewer():
    from cyclecontrol.comments import approval_line

    items = [
        ReviewItem("ci:abc12345", "ci", "CI", False, False, "CI is failing"),
        ReviewItem("conflict:abc12345", "conflict", "GitHub", False, False, "conflicts"),
        ReviewItem("t:1", "thread", "rio", False, False, "@rio: rename"),
    ]
    line = approval_line({"approved": False, "blocking": [], "approvers": []}, items)
    assert "@rio" in line and "@CI" not in line and "@GitHub" not in line


def test_codecov_is_not_ci():
    from cyclecontrol.comments import effective_ci

    rollup = {
        "state": "FAILURE",
        "contexts": {
            "nodes": [
                {"__typename": "StatusContext", "context": "codecov/patch", "state": "FAILURE"},
                {
                    "__typename": "CheckRun",
                    "name": "ci/circleci: test-php",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                },
            ]
        },
    }
    assert effective_ci(rollup) == "SUCCESS"


def test_a_conflicting_head_gets_a_conflict_item_and_no_ci_item(rig, monkeypatch):
    from tests.test_approval import full_pr

    watcher, manager, session, fake, _clock, _settings = rig
    (session.git_dir / "spp-phase.json").write_text(json.dumps({"phase": "human"}))
    fake.raw = full_pr(
        headRefOid="ef3763b7aa",
        mergeable="CONFLICTING",
        commits={
            "nodes": [
                {
                    "commit": {
                        "oid": "ef3763b7aa",
                        "statusCheckRollup": {
                            "state": "FAILURE",
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "ci/circleci: test-php-browser",
                                        "status": "COMPLETED",
                                        "conclusion": "FAILURE",
                                    }
                                ]
                            },
                        },
                    }
                }
            ]
        },
    )
    monkeypatch.setattr(manager, "wake", lambda *a, **k: "running")
    watcher.poll()
    items = list(watcher._store.pr("o/abc#7")["items"])
    assert "conflict:ef3763b7" in items and "ci:ef3763b7" not in items


def test_threads_opened_after_the_merge_are_announced_once_and_flags_are_dropped(rig):
    watcher, _, _session, fake, _clock, _ = rig
    told = []
    watcher._notify = lambda title, msg: told.append((title, msg))
    watcher._store.set_notified("needs:o/abc#7:t:1", True)
    watcher._store.set_notified("ready:o/abc#7", False)
    watcher._store.set_notified("requeue-try:o/abc#7:abc:1", True)
    watcher._store.set_notified("ready:o/abc#8", True)
    late = thread("T1", [c(1, "sam", "py-12 is hardcoded", created="2026-10-22T10:21:56Z")])
    late["comments"]["nodes"][0]["url"] = "https://x/late"
    early = thread("T2", [c(2, "sam", "older", created="2026-10-22T09:00:00Z")])
    fake.raw = {**pr(threads=[late, early], state="MERGED"), "mergedAt": "2026-10-22T10:03:39Z"}
    watcher.poll()
    watcher.poll()
    assert told == [("ABC-12 got 1 review thread after merging", "https://x/late")]
    assert list(watcher._store.flags) == ["ready:o/abc#8", "late-threads:o/abc#7"]


def test_delivery_writes_the_pr_pointer_for_the_reply_script(rig):
    watcher, _, session, fake, _, _ = rig
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename")])])
    watcher.poll()
    watcher.tick()
    assert json.loads((session.git_dir / "spp-pr.json").read_text()) == {
        "repo": "o/abc",
        "number": 7,
    }


def test_a_thread_on_the_stacked_pr_is_polled_delivered_and_shown(rig):
    watcher, _, session, _fake, _clock, _ = rig
    session.pty.tail = lambda: (
        b"Backend: https://github.com/o/abc/pull/7\nFrontend, stacked: https://github.com/o/abc/pull/8\n"
    )
    raws = {7: pr(), 8: {**pr(), "number": 8}}
    fetched = []

    def fetch(repo, number):
        fetched.append(number)
        return raws[number]

    watcher._fetch = fetch
    assert watcher.poll() == [] and fetched == [7, 8]
    assert watcher._store.stacked["ABC-12"] == [8]
    raws[8] = {**pr(threads=[thread("T9", [c(9, "sam", "Guard the save")])]), "number": 8}
    assert watcher.poll() == ["ABC-12"]
    assert watcher._store.pending_items("o/abc#8") and not watcher._store.pending_items("o/abc#7")
    session.pty.last_output = 0.0
    assert watcher.tick() == ["ABC-12"]
    assert "PR #8" in session.pty.sent[0].decode()
    view = watcher.view(session)
    assert [r["key"] for r in view["items"]] == ["t:9"] and view["waiting"] == 1
    session.pty.tail = lambda: b""
    watcher._store.save()
    from cyclecontrol.comments import CommentStore

    assert CommentStore(watcher._store._path).stacked == {"ABC-12": [8]}


def test_a_stack_finishes_only_when_every_pr_in_it_is_closed(rig):
    watcher, _manager, session, _fake, _clock, _ = rig
    session.pty.tail = lambda: (
        b"Backend: https://github.com/o/abc/pull/7\nFrontend: https://github.com/o/abc/pull/8\n"
    )
    raws = {7: pr(), 8: {**pr(), "number": 8}}
    watcher._fetch = lambda repo, number: raws[number]
    watcher.poll()
    raws[7] = pr(state="MERGED")
    raws[8] = {**pr(threads=[thread("T9", [c(9, "sam", "Guard the save")])]), "number": 8}
    assert watcher.poll() == ["ABC-12"]
    session.pty.last_output = 0.0
    assert watcher.tick() == ["ABC-12"] and not session.finished
    assert "PR #8" in session.pty.sent[0].decode()
    raws[8] = {**pr(state="MERGED"), "number": 8}
    watcher.poll()
    watcher.tick()
    assert session.finished


def test_feedback_held_behind_a_busy_builder_reports_how_long_it_has_waited(rig):
    watcher, _, session, fake, clock, _ = rig
    fake.raw = pr(threads=[thread("T9", [c(9, "sam", "Guard the save")])])
    watcher.poll()
    assert watcher.view(session)["heldSince"] == 1000.0
    clock["now"] = 1700.0
    session.pty.last_output = time.time()
    assert watcher.tick() == [] and watcher.view(session)["heldSince"] == 1000.0
    session.pty.last_output = 0.0
    watcher.tick()
    assert watcher.view(session)["heldSince"] == 0


def test_a_stacked_pr_that_becomes_ready_is_announced_with_its_number(rig):
    watcher, _, session, _fake, _clock, _ = rig
    session.pty.tail = lambda: (
        b"Backend: https://github.com/o/abc/pull/7\nFrontend: https://github.com/o/abc/pull/8\n"
    )
    notes = []
    watcher._notify = lambda title, message: notes.append((title, message))
    ready = {
        **pr(reviews=[{"author": {"login": "sam"}, "state": "APPROVED", "body": ""}]),
        "number": 8,
        "reviewDecision": "APPROVED",
        "mergeable": "MERGEABLE",
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": "abc",
                        "statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": []}},
                    }
                }
            ]
        },
    }
    raws = {7: pr(), 8: ready}
    watcher._fetch = lambda repo, number: raws[number]
    watcher.poll()
    watcher.tick()
    assert ("ABC-12 is ready to merge", "PR #8: " + session.issue.title) in notes


class FakeCircle:
    def __init__(self, infra=True):
        self.enabled = True
        self.infra = infra
        self.reruns = []
        self.reads = 0

    def failure(self, repo, url):
        from cyclecontrol.circleci import Failure

        self.reads += 1
        step = "Fetching merge base with develop" if self.infra else "Run jest"
        tail = (
            "fatal: shallow file has changed since we read it"
            if self.infra
            else "expected 1 to be 2"
        )
        return Failure("wf-1", "lint-js-style", step, tail)

    def rerun_from_failed(self, wf):
        self.reruns.append(wf)
        return True


def _red_pr(head="aef6849f0000"):
    return {
        **pr(),
        "headRefOid": head,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "statusCheckRollup": {
                            "state": "FAILURE",
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "StatusContext",
                                        "context": "ci/circleci: lint-js-style",
                                        "state": "FAILURE",
                                        "targetUrl": "https://app.circleci.com/workflow/wf-1/job/j-1",
                                    },
                                ]
                            },
                        },
                    }
                }
            ]
        },
    }


def _handed_over(session):
    (session.git_dir / "spp-phase.json").write_text(json.dumps({"phase": "human"}))


def test_an_infrastructure_ci_failure_is_re_run_twice_before_the_builder_hears_of_it(rig):
    watcher, _, session, fake, _clock, _ = rig
    circle = FakeCircle()
    watcher._circleci = circle
    _handed_over(session)
    fake.raw = _red_pr()
    assert watcher.poll() == [] and circle.reruns == ["wf-1"]
    assert not watcher._store.pending_items("o/abc#7")
    assert watcher._store.state_of("o/abc#7")["ciReruns"] == 1
    assert watcher.view(session)["stage"]["headline"] == "CI is failing"
    assert watcher.poll() == [] and circle.reruns == ["wf-1", "wf-1"]
    assert watcher.poll() == ["ABC-12"] and circle.reruns == ["wf-1", "wf-1"]
    [item] = watcher._store.pending_items("o/abc#7")
    assert (
        item.kind == "ci" and "already re-ran 2 times" in item.body and "shallow file" in item.body
    )


def test_a_real_test_failure_goes_straight_to_the_builder_and_the_log_is_read_once(rig):
    watcher, _, session, fake, _clock, _ = rig
    circle = FakeCircle(infra=False)
    watcher._circleci = circle
    _handed_over(session)
    fake.raw = _red_pr()
    assert watcher.poll() == ["ABC-12"] and circle.reruns == []
    watcher.poll()
    assert circle.reads == 1
    [item] = watcher._store.pending_items("o/abc#7")
    assert item.kind == "ci" and "already re-ran" not in item.body


def test_a_pr_someone_else_opened_is_left_alone(rig):
    watcher, _, _, fake, _, settings = rig
    settings.github_login = "Owner"
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])], author="matt")
    assert watcher.poll() == []
    assert watcher._store.pending_items("o/abc#7") == []
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])], author="owner")
    assert watcher.poll() == ["ABC-12"]


def test_a_delivered_thread_already_resolved_on_github_is_not_counted_as_waiting():
    item = {
        "key": "t:1",
        "kind": "thread",
        "author": "coderabbitai",
        "body": "fix it",
        "thread_id": "PRRT_1",
        "delivered_at": 1.0,
    }
    open_item = {**item, "key": "t:2", "thread_id": "PRRT_2"}
    store_pr = {
        "items": {"t:1": item, "t:2": open_item},
        "pending": [],
        "resolutions": {},
        "resolved_threads": ["PRRT_1"],
        "state": {"labels": [], "unresolved": 1},
    }

    view = comments_view(store_pr, {})

    states = {row["key"]: row["state"] for row in view["items"]}
    assert states == {"t:1": "answered", "t:2": "delivered"}
    assert view["waiting"] == 1 and view["addressed"] == 1


def test_drop_truncated_links_keeps_only_the_full_number():
    from cyclecontrol.sessions import drop_truncated

    base = "https://github.com/AgentSoftware/street/pull/"
    assert drop_truncated([base + "36", base + "36269", base + "36306"]) == [
        base + "36269",
        base + "36306",
    ]


def test_a_new_head_is_judged_ready_afresh(rig):
    from cyclecontrol.comments import pr_state
    from tests.test_approval import full_pr

    watcher, _, _session, _fake, _, _ = rig
    notes = []
    watcher._notify = lambda title, message: notes.append(title)
    watcher.poll()
    state = pr_state(full_pr())
    watcher._store.set_state("o/abc#7", {**state, "head": "aaa"})
    watcher.tick()
    watcher.tick()
    assert notes == ["ABC-12 is ready to merge"]
    watcher._store.set_state("o/abc#7", {**state, "head": "bbb"})
    watcher.tick()
    assert notes == ["ABC-12 is ready to merge"] * 2


def test_review_hand_offs_stop_after_three_rounds_and_tell_you_once(rig):
    watcher, _, session, fake, _clock, _ = rig
    notes = []
    watcher._notify = lambda title, message: notes.append(title)
    watcher.poll()
    fake.raw = pr(threads=[thread("T1", [c(1, "sam", "Rename this")])])
    watcher.poll()
    session.pty.last_output = 0.0
    watcher._store.set_notified("rounds:o/abc#7", 3)
    assert watcher.tick() == [] and session.pty.sent == []
    assert watcher.tick() == []
    assert [n for n in notes if "3 review rounds" in n] == [
        "ABC-12 needs you: 3 review rounds and still not clean"
    ]


def test_a_closed_pr_by_someone_else_is_recorded_so_the_ticket_can_finish(rig):
    watcher, _, _, fake, _, settings = rig
    settings.github_login = "Owner"
    fake.raw = pr(state="CLOSED", author="matt")
    watcher.poll()
    state = watcher._store.state_of("o/abc#7")
    assert state and state["open"] is False
