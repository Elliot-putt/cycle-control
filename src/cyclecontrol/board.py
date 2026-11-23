"""The latest Linear snapshot, refreshed on demand and on a timer."""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from cyclecontrol.config import Settings
from cyclecontrol.linear import LinearClient, LinearError
from cyclecontrol.models import Blocker, Cycle, Issue

logger = logging.getLogger(__name__)


def _demo(
    issue_id,
    title,
    status,
    status_type,
    team="PP",
    mine=True,
    in_cycle=True,
    parent="",
    children=False,
    blockers=(),
    priority=0,
):
    return Issue(
        issue_id,
        title,
        f"https://linear.app/demo/issue/{issue_id}",
        f"feature/{issue_id.lower()}",
        status,
        status_type,
        "Elliot Putt" if mine else "Sam Lee",
        in_cycle,
        mine,
        priority=priority,
        team_key=team,
        parent_id=parent,
        has_children=children,
        blocked_by=tuple(Blocker(b, "backlog") for b in blockers),
        created_at=f"2026-10-{10 + len(issue_id) % 9:02d}",
    )


DEMO_ISSUES = [
    _demo(
        "PP-1035",
        "Move frontend date handling from dayjs to date-fns",
        "Todo",
        "unstarted",
        in_cycle=False,
    ),
    _demo("PP-1030", "Contractor mobile: job completed screen polish", "In Progress", "started"),
    _demo(
        "PP-1028", "Retry failed EventBridge publishes", "Todo", "unstarted", mine=False, priority=2
    ),
    _demo("PP-1021", "Referral assignment fallback tests", "Done", "completed"),
    _demo("PP-200", "Rework referral reminders", "In Progress", "started", children=True),
    _demo("PP-201", "Reminders PR 1: schedule model", "Backlog", "backlog", parent="PP-200"),
    _demo(
        "PP-202", "Reminders PR 2: notification templates", "Backlog", "backlog", parent="PP-200"
    ),
    _demo(
        "PP-203",
        "Reminders PR 3: send job",
        "Backlog",
        "backlog",
        parent="PP-200",
        blockers=("PP-201", "PP-202"),
    ),
    _demo(
        "WEB-5",
        "Rapid brand switch on the sync screen",
        "Todo",
        "unstarted",
        team="WEB",
        in_cycle=False,
    ),
]


class Board:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.cycle: Cycle | None = None
        self.issues: list[Issue] = []
        self.error = ""
        self.updated_at = 0.0
        self._overrides: dict[str, tuple[str, str]] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.linear_api_key) or self._settings.demo

    def refresh(self) -> None:
        if self._settings.demo:
            self.cycle = Cycle(number=42, days_left=5, done=1, total=9)
            self.issues = list(DEMO_ISSUES)
            self.error = ""
            self.updated_at = time.time()
            return
        if not self._settings.linear_api_key:
            return
        try:
            client = LinearClient(self._settings.linear_api_key, self._settings.linear_team_key)
            self.cycle, self.issues = client.fetch_dashboard()
            self.error = ""
            self.updated_at = time.time()
            self._overrides = {
                issue_id: value
                for issue_id, value in self._overrides.items()
                if not any(i.id == issue_id and i.is_started for i in self.issues)
            }
        except (LinearError, OSError, ValueError) as failure:
            logger.warning("Linear refresh failed: %s", failure)
            self.error = f"Linear: {failure}"
        except Exception as failure:
            logger.exception("Linear refresh crashed")
            self.error = f"Linear: {failure}"

    def mark_in_progress(self, issue_id: str) -> None:
        self._overrides[issue_id] = ("In Progress", "started")
        if self._settings.demo or not self._settings.linear_api_key:
            return
        try:
            LinearClient(
                self._settings.linear_api_key, self._settings.linear_team_key
            ).mark_in_progress(issue_id)
        except Exception:
            logger.exception("Could not move %s to In Progress", issue_id)

    def forget(self, issue_id: str) -> None:
        """Drop the row until the next refresh brings it back, if Linear still lists it."""
        self.issues = [i for i in self.issues if i.id != issue_id]
        self._overrides.pop(issue_id, None)

    def view(self) -> list[Issue]:
        shown: list[Issue] = []
        for issue in self.issues:
            if issue.is_cancelled:
                continue
            override = self._overrides.get(issue.id)
            shown.append(
                replace(issue, status=override[0], status_type=override[1]) if override else issue
            )
        return shown

    def find(self, issue_id: str) -> Issue | None:
        return next((i for i in self.view() if i.id == issue_id), None)

    def parents(self) -> list[Issue]:
        issues = self.view()
        ids = {i.parent_id for i in issues if i.parent_id} | {
            i.id for i in issues if i.has_children
        }
        return [i for i in issues if i.id in ids]

    def children_of(self, parent_id: str) -> list[Issue]:
        return [i for i in self.view() if i.parent_id == parent_id]
