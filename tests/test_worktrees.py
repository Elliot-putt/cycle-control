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


def _git(*args, cwd):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_stacked_base_missing_from_the_team_repo_starts_from_its_default_base(tmp_path):
    from cyclecontrol.config import TeamConfig
    from cyclecontrol.models import Issue

    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "develop", cwd=origin)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
        cwd=origin,
    )
    repo = tmp_path / "repo"
    _git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    team = TeamConfig("T", repo, "o/r", "spp-builder", "develop", "wt")
    issue = Issue(
        "T-9", "t", "u", "feature/t-9", "Todo", "unstarted", "", False, True, team_key="T"
    )
    path = worktrees.ensure_worktree(team, issue, "feature/other-repo-branch")
    assert path.exists()
