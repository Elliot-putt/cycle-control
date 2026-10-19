import json
import time

import pytest

from cyclecontrol import commentwatch
from cyclecontrol.comments import (
    MARKER,
    CommentStore,
    comments_view,
    feedback_text,
    parse_pr,
    read_actions,
)
from cyclecontrol.commentwatch import CommentWatcher
from cyclecontrol.github import GithubError
from cyclecontrol.sessions import SessionManager
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
    assert keys(items) == ["c:13", "c:14", "c:15"]
    assert items[1].is_owner and not items[0].is_owner


def test_review_bodies_count_for_requested_changes_and_coderabbit_sections_only():
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
    assert keys(items) == ["r:21", "r:23"]


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
    assert keys(items) == ["c:42", "c:43"]
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
