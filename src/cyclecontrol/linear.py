"""Linear GraphQL client: the current cycle plus everything assigned to you."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from cyclecontrol.models import Blocker, Cycle, Issue

logger = logging.getLogger(__name__)

API_URL = "https://api.linear.app/graphql"

ISSUE_FIELDS = """
fragment IssueFields on Issue {
  identifier title url branchName priority createdAt
  team { key }
  parent { identifier }
  children(first: 1) { nodes { identifier } }
  state { name type }
  assignee { id name }
}
"""

FAMILY_FIELDS = """
fragment FamilyFields on Issue {
  identifier title url branchName priority createdAt
  team { key }
  parent { identifier }
  state { name type }
  assignee { id name }
  inverseRelations(first: 8) { nodes { type issue { identifier state { type } } } }
}
"""
FAMILY_BATCH = 4

DASHBOARD_QUERY = (
    """
query Dashboard($team: String!) {
  viewer {
    id
    assignedIssues(first: 100, filter: {state: {type: {nin: ["completed", "canceled"]}}}) {
      nodes { ...IssueFields }
    }
  }
  cycles(first: 1, filter: {team: {key: {eq: $team}}, isActive: {eq: true}}) {
    nodes { number endsAt issues(first: 200) { nodes { ...IssueFields } } }
  }
}
"""
    + ISSUE_FIELDS
)

STARTED_STATES_QUERY = """
query Started($team: String!) {
  workflowStates(filter: {team: {key: {eq: $team}}, type: {eq: "started"}}) {
    nodes { id name position }
  }
}
"""

UPDATE_MUTATION = """
mutation Move($id: String!, $state: String!) {
  issueUpdate(id: $id, input: {stateId: $state}) { success }
}
"""


IDENTIFIER = re.compile(r"^[A-Za-z0-9]+-\d+$")


def family_query(parent_ids: list[str]) -> str:
    """One query that returns each parent and all its sub-issues, with their blockers."""
    safe = [pid for pid in parent_ids if IDENTIFIER.match(pid)]
    aliases = "\n".join(
        f'  p{i}: issue(id: "{pid}") {{ ...FamilyFields children(first: 30) {{ nodes {{ ...FamilyFields }} }} }}'
        for i, pid in enumerate(safe)
    )
    return "query Family {\n" + aliases + "\n}\n" + FAMILY_FIELDS


def merge_family(issues: list[Issue], family: dict[str, Any], mine_ids: set[str]) -> list[Issue]:
    """Add parents and sub-issues the first query missed, and give every family member its blockers."""
    by_id = {i.id: i for i in issues}
    order = [i.id for i in issues]
    for node in family.values():
        if not node:
            continue
        for raw in [node, *node.get("children", {}).get("nodes", [])]:
            fresh = _issue(raw, False, mine_ids)
            current = by_id.get(fresh.id)
            if current is None:
                by_id[fresh.id] = replace(fresh, has_children=raw is node)
                order.append(fresh.id)
            else:
                by_id[fresh.id] = replace(
                    current, blocked_by=fresh.blocked_by, parent_id=fresh.parent_id
                )
    return [by_id[i] for i in order]


class LinearError(RuntimeError):
    pass


def _issue(node: dict[str, Any], in_cycle: bool, mine_ids: set[str]) -> Issue:
    state = node.get("state") or {}
    assignee = node.get("assignee") or {}
    blocked_by = tuple(
        Blocker(rel["issue"]["identifier"], (rel["issue"].get("state") or {}).get("type", ""))
        for rel in (node.get("inverseRelations") or {}).get("nodes", [])
        if rel.get("type") == "blocks" and rel.get("issue")
    )
    return Issue(
        id=node["identifier"],
        title=node.get("title", ""),
        url=node.get("url", ""),
        branch=node.get("branchName") or f"feature/{node['identifier'].lower()}",
        status=state.get("name", ""),
        status_type=state.get("type", ""),
        assignee=assignee.get("name", ""),
        in_cycle=in_cycle,
        is_mine=node["identifier"] in mine_ids,
        priority=int(node.get("priority") or 0),
        team_key=(node.get("team") or {}).get("key", ""),
        parent_id=(node.get("parent") or {}).get("identifier", ""),
        has_children=bool((node.get("children") or {}).get("nodes")),
        blocked_by=blocked_by,
        created_at=node.get("createdAt", ""),
    )


def parse_dashboard(
    data: dict[str, Any], now: datetime | None = None
) -> tuple[Cycle | None, list[Issue]]:
    """Turn the dashboard query result into the cycle header and one flat issue list."""
    now = now or datetime.now(UTC)
    mine_nodes = data["viewer"]["assignedIssues"]["nodes"]
    mine_ids = {node["identifier"] for node in mine_nodes}
    cycle_nodes = data["cycles"]["nodes"]
    cycle_issues: list[Issue] = []
    cycle: Cycle | None = None
    if cycle_nodes:
        raw = cycle_nodes[0]
        cycle_issues = [_issue(n, True, mine_ids) for n in raw["issues"]["nodes"]]
        ends = datetime.fromisoformat(raw["endsAt"])
        cycle = Cycle(
            number=raw["number"],
            days_left=max(0, math.ceil((ends - now).total_seconds() / 86400)),
            done=sum(1 for i in cycle_issues if not i.is_open),
            total=len(cycle_issues),
        )
    in_cycle_ids = {i.id for i in cycle_issues}
    off_cycle = [
        _issue(n, False, mine_ids) for n in mine_nodes if n["identifier"] not in in_cycle_ids
    ]
    return cycle, cycle_issues + off_cycle


class LinearClient:
    def __init__(self, api_key: str, team_key: str) -> None:
        self._team = team_key
        self._http = httpx.Client(
            base_url=API_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=30,
        )

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._http.post("", json={"query": query, "variables": variables})
        if response.status_code == 401:
            raise LinearError("Linear rejected the API key.")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise LinearError(body["errors"][0].get("message", "Linear query failed"))
        return body["data"]

    def fetch_dashboard(self) -> tuple[Cycle | None, list[Issue]]:
        data = self._post(DASHBOARD_QUERY, {"team": self._team})
        cycle, issues = parse_dashboard(data)
        parent_ids = sorted(
            {i.parent_id for i in issues if i.parent_id} | {i.id for i in issues if i.has_children}
        )
        if parent_ids:
            mine_ids = {node["identifier"] for node in data["viewer"]["assignedIssues"]["nodes"]}
            for start in range(0, len(parent_ids), FAMILY_BATCH):
                family = self._post(family_query(parent_ids[start : start + FAMILY_BATCH]), {})
                issues = merge_family(issues, family, mine_ids)
        return cycle, issues

    def mark_in_progress(self, issue_id: str) -> None:
        states = self._post(STARTED_STATES_QUERY, {"team": self._team})["workflowStates"]["nodes"]
        if not states:
            return
        first = min(states, key=lambda s: s["position"])
        self._post(UPDATE_MUTATION, {"id": issue_id, "state": first["id"]})
