"""Settings from the repo's .env file, and the Linear team to repo mapping from teams.json."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings

ROOT = Path(__file__).parents[2]
ENV_PATH = ROOT / ".env"


@dataclass(frozen=True)
class TeamConfig:
    key: str
    repo_path: Path
    github_repo: str
    agent: str
    base_branch: str = "develop"
    worktree_prefix: str = "spp"

    def worktree_path(self, issue_id: str) -> Path:
        number = issue_id.rsplit("-", 1)[-1]
        return self.repo_path.parent / f"{self.worktree_prefix}-{number}"


def load_teams(path: Path) -> dict[str, TeamConfig]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    teams: dict[str, TeamConfig] = {}
    for key, entry in raw.items():
        teams[key] = TeamConfig(
            key=key,
            repo_path=Path(entry["repo"]).expanduser(),
            github_repo=entry["github"],
            agent=entry["agent"],
            base_branch=entry.get("baseBranch", "develop"),
            worktree_prefix=entry.get("worktreePrefix", "spp"),
        )
    return teams


class Settings(BaseSettings):
    linear_api_key: str = ""
    linear_team_key: str = "PP"
    claude_bin: str = "claude"
    claude_model: str = ""
    skip_permissions: bool = True
    waiting_after_seconds: int = 20
    port: int = 8770
    slack_channel_id: str = "C0C7J0A1S3B"
    merge_label: str = "mergequeue"
    state_file: str = "~/.cycle-control/sessions.json"
    teams_file: str = str(ROOT / "teams.json")
    max_builders: int = 3
    auto_rebase: bool = False
    auto_comments: bool = True
    auto_requeue: bool = True
    comment_poll_seconds: int = 300
    comments_file: str = "~/.cycle-control/comments.json"
    rebase_poll_seconds: int = 60
    demo: bool = False

    model_config = {"env_file": str(ENV_PATH), "extra": "ignore"}

    @property
    def state_path(self) -> Path:
        return Path(self.state_file).expanduser()

    @property
    def comments_path(self) -> Path:
        return Path(self.comments_file).expanduser()

    @property
    def teams(self) -> dict[str, TeamConfig]:
        return load_teams(Path(self.teams_file).expanduser())

    def team_for(self, team_key: str) -> TeamConfig | None:
        return self.teams.get(team_key)


def save_env_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    """Set one key in the .env file, keeping every other line as it was."""
    lines = path.read_text().splitlines() if path.exists() else []
    out: list[str] = []
    seen = False
    for line in lines:
        if line.partition("=")[0].strip() == key:
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
