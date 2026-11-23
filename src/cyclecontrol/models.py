"""Plain data shapes shared by the Linear client, the sessions and the server."""

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["plan", "auto"]
Health = Literal["working", "waiting", "done"]


@dataclass(frozen=True)
class Blocker:
    id: str
    status_type: str


@dataclass(frozen=True)
class Issue:
    id: str
    title: str
    url: str
    branch: str
    status: str
    status_type: str
    assignee: str
    in_cycle: bool
    is_mine: bool
    priority: int = 0
    team_key: str = ""
    parent_id: str = ""
    has_children: bool = False
    blocked_by: tuple[Blocker, ...] = ()
    created_at: str = ""

    @property
    def team(self) -> str:
        return self.team_key or self.id.rsplit("-", 1)[0]

    @classmethod
    def from_dict(cls, data: dict) -> "Issue":
        known = {f for f in cls.__dataclass_fields__}
        fields = {k: v for k, v in data.items() if k in known}
        fields["blocked_by"] = tuple(Blocker(**b) for b in data.get("blocked_by", []))
        return cls(**fields)

    @property
    def is_open(self) -> bool:
        return self.status_type not in ("completed", "canceled")

    @property
    def is_cancelled(self) -> bool:
        return self.status_type == "canceled"

    @property
    def is_started(self) -> bool:
        return self.status_type == "started"


@dataclass(frozen=True)
class Cycle:
    number: int
    days_left: int
    done: int
    total: int


@dataclass(frozen=True)
class PlanStep:
    id: str
    label: str
    kind: str
    files: list[str] = field(default_factory=list)
    reuses: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Progress:
    phase: str = "starting"
    note: str = ""
    summary: str = ""
    plan: list[PlanStep] = field(default_factory=list)
    gates: Literal["pass", "block", "none"] = "none"
    review: Literal["PASS", "CHANGES", "none"] = "none"
    gate_blocks: list[str] = field(default_factory=list)
    gate_warns: list[str] = field(default_factory=list)
    review_findings: list[str] = field(default_factory=list)
    pr_url: str = ""
