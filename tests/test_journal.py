import json
import os

from cyclecontrol import journal
from cyclecontrol.journal import KEEP, Journal
from cyclecontrol.sessions import notify


def test_a_record_is_kept_on_disk_with_its_issue_and_read_back_newest_first(tmp_path):
    clock = {"now": 100.0}
    book = Journal(tmp_path / "j.jsonl", lambda: clock["now"])
    book.record("ABC-12 is picking up 2 review comments", "From @sam")
    clock["now"] = 200.0
    book.record("Back after 40 min asleep", "Checking every PR now")
    assert [e["issue"] for e in book.recent()] == ["", "ABC-12"]
    assert [e["at"] for e in book.recent()] == [200.0, 100.0]
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["title"] == "ABC-12 is picking up 2 review comments"
    assert Journal(tmp_path / "j.jsonl").recent(1)[0]["message"] == "Checking every PR now"


def test_the_journal_never_grows_past_its_keep(tmp_path):
    book = Journal(tmp_path / "j.jsonl")
    for n in range(KEEP + 25):
        book.record(f"XY-{n} note")
    assert len(book.recent(10_000)) == KEEP
    assert Journal(tmp_path / "j.jsonl").recent(1)[0]["title"] == f"XY-{KEEP + 24} note"


def test_every_desktop_notification_also_lands_in_the_installed_journal(tmp_path, monkeypatch):
    monkeypatch.setitem(os.environ, "CYCLE_NO_NOTIFY", "1")
    book = Journal(tmp_path / "j.jsonl")
    journal.install(book)
    try:
        notify("ABC-12 is ready to merge", "PR #7")
        journal.note("ABC-12 has a merge conflict on PR #7", "Handed to the builder")
    finally:
        journal.install(None)
    assert [e["title"] for e in book.recent()] == [
        "ABC-12 has a merge conflict on PR #7",
        "ABC-12 is ready to merge",
    ]
    notify("Nothing installed", "goes nowhere")
    assert len(book.recent()) == 2
