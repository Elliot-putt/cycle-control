import subprocess
from pathlib import Path

import pytest

from cyclecontrol.rebase import RebaseAction, RebaseCheck, check_rebase, decide


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "init.defaultBranch=develop",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    commit(path, "a.txt", "one\ntwo\nthree\n", "init")
    git(path, "checkout", "-q", "-b", "feature/base")
    commit(path, "b.txt", "b1\nb2\nb3\n", "base work")
    return path


def make_child(repo: Path, file: str, text: str) -> tuple[str, str]:
    """A child branch stacked on feature/base. Returns (base tip it was built on, child tip)."""
    base_tip = git(repo, "rev-parse", "feature/base")
    git(repo, "checkout", "-q", "-b", "feature/child")
    child_tip = commit(repo, file, text, "child work")
    git(repo, "checkout", "-q", "feature/base")
    return base_tip, child_tip


def worktrees(repo: Path) -> list[str]:
    return [line for line in git(repo, "worktree", "list").splitlines()]


def test_base_moved_with_unrelated_changes_rebases_clean(repo):
    old_base, child_tip = make_child(repo, "c.txt", "child\n")
    new_base = commit(repo, "d.txt", "later base work\n", "base moves")
    assert check_rebase(repo, "feature/child", old_base, new_base) == RebaseCheck(True)
    assert git(repo, "rev-parse", "feature/child") == child_tip
    assert len(worktrees(repo)) == 1


def test_base_moved_on_the_same_lines_is_a_conflict_and_names_the_file(repo):
    old_base, child_tip = make_child(repo, "b.txt", "b1\nCHILD\nb3\n")
    new_base = commit(repo, "b.txt", "b1\nBASE\nb3\n", "base edits the same line")
    result = check_rebase(repo, "feature/child", old_base, new_base)
    assert result.clean is False and result.conflicts == ("b.txt",)
    assert git(repo, "rev-parse", "feature/child") == child_tip
    assert len(worktrees(repo)) == 1
    assert git(repo, "status", "--porcelain") == ""


def test_base_squash_merged_into_develop_rebases_clean_with_onto(repo):
    old_base, child_tip = make_child(repo, "c.txt", "child\n")
    git(repo, "checkout", "-q", "develop")
    git(repo, "merge", "--squash", "feature/base")
    git(repo, "commit", "-q", "-m", "squash of base")
    develop_tip = git(repo, "rev-parse", "HEAD")
    assert check_rebase(repo, "feature/child", old_base, develop_tip).clean is True
    assert git(repo, "rev-parse", "feature/child") == child_tip


def test_squash_merge_then_a_conflicting_develop_change_is_a_conflict(repo):
    old_base, _ = make_child(repo, "b.txt", "b1\nCHILD\nb3\n")
    git(repo, "checkout", "-q", "develop")
    git(repo, "merge", "--squash", "feature/base")
    git(repo, "commit", "-q", "-m", "squash of base")
    develop_tip = commit(repo, "b.txt", "b1\nOTHER\nb3\n", "develop edits the same line")
    result = check_rebase(repo, "feature/child", old_base, develop_tip)
    assert result.clean is False and result.conflicts == ("b.txt",)


def never(*_):
    raise AssertionError("no check expected")


def clean(*_):
    return RebaseCheck(True)


def conflict(*_):
    return RebaseCheck(False, ("x.txt",))


def test_decide_does_nothing_when_the_base_has_not_moved():
    assert decide("feature/base", "aaa", "develop", "aaa", False, "ddd", never) == RebaseAction(
        "none"
    )
    assert decide("develop", "aaa", "develop", "bbb", True, "ddd", never) == RebaseAction("none")


def test_decide_tells_the_builder_to_rebase_when_the_base_moved_and_it_is_clean():
    action = decide("feature/base", "aaa", "develop", "bbb", False, "ddd", clean)
    assert (action.kind, action.new_base, action.new_sha) == ("rebase", "feature/base", "bbb")
    assert (
        "git rebase --onto origin/feature/base aaa" in action.text
        and "force-with-lease" in action.text
    )
    assert "gh pr edit" not in action.text


def test_decide_retargets_to_develop_when_the_base_pr_merged_and_it_is_clean():
    action = decide("feature/base", "aaa", "develop", "", True, "ddd", clean)
    assert (action.kind, action.new_base, action.new_sha) == ("rebase", "develop", "ddd")
    assert (
        "git rebase --onto origin/develop aaa" in action.text
        and "gh pr edit --base develop" in action.text
    )


def test_decide_never_hands_a_conflict_to_the_agent():
    action = decide("feature/base", "aaa", "develop", "bbb", False, "ddd", conflict)
    assert action.kind == "conflict" and action.files == ("x.txt",) and "rebase" not in action.text
    merged = decide("feature/base", "aaa", "develop", "", True, "ddd", conflict)
    assert merged.kind == "conflict" and merged.new_base == "develop"
