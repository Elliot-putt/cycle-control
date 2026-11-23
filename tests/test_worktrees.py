import pytest

from cyclecontrol import worktrees


def _just(tmp_path, recipe: str):
    (tmp_path / "justfile").write_text(recipe)


def test_provision_skips_a_repo_without_the_recipe(tmp_path):
    _just(tmp_path, "build:\n\techo hi\n")
    assert worktrees.provision(tmp_path) is False


def test_provision_runs_the_recipe_when_present(tmp_path):
    _just(tmp_path, "worktree-provision:\n\ttouch ran\n")
    assert worktrees.provision(tmp_path) is True
    assert (tmp_path / "ran").exists()


def test_provision_still_raises_when_the_recipe_fails(tmp_path):
    _just(tmp_path, "worktree-provision:\n\texit 3\n")
    with pytest.raises(worktrees.WorktreeError):
        worktrees.provision(tmp_path)
