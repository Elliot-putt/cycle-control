"""The PR of a builder's branch, and the merge label, through the gh CLI."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


class GithubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    is_draft: bool
    labels: list[str]


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=False, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise GithubError(result.stderr.strip() or "gh failed")
    return result.stdout


def find_open_pr(repo: str, branch: str) -> PullRequest:
    out = _gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--limit",
        "1",
        "--json",
        "number,title,url,isDraft,labels",
    )
    found = json.loads(out)
    if not found:
        raise GithubError(f"No open PR for {branch}.")
    pr = found[0]
    return PullRequest(
        pr["number"],
        pr["title"],
        pr["url"],
        pr["isDraft"],
        [label["name"] for label in pr["labels"]],
    )


def add_label(repo: str, number: int, label: str) -> None:
    _gh("pr", "edit", str(number), "--repo", repo, "--add-label", label)


def find_pr_any_state(repo: str, branch: str) -> PrState | None:
    """The latest PR (open, merged or closed) whose head is the branch."""
    out = _gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "all",
        "--limit",
        "1",
        "--json",
        "number,url,headRefName,state,baseRefName,headRefOid",
    )
    found = json.loads(out)
    if not found:
        return None
    pr = found[0]
    return PrState(
        pr["number"], pr["url"], pr["headRefName"], pr["state"], pr["baseRefName"], pr["headRefOid"]
    )


@dataclass(frozen=True)
class PrState:
    number: int
    url: str
    branch: str
    state: str
    base: str
    head_sha: str


PR_FEEDBACK_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number state isDraft headRefName headRefOid reviewDecision author { login }
      labels(first: 30) { nodes { name } }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
      reviewThreads(first: 100) { nodes { id isResolved isOutdated path line
        comments(first: 30) { nodes { databaseId author { login __typename } body url createdAt } } } }
      comments(last: 50) { nodes { databaseId author { login __typename } body url createdAt } }
      reviews(last: 30) { nodes { databaseId author { login __typename } state body url submittedAt } }
    }
  }
}
"""


def fetch_pr_feedback(repo: str, number: int) -> dict:
    """Review threads, top-level comments and review bodies of one PR, in a single gh api call."""
    owner, name = repo.split("/", 1)
    out = _gh(
        "api",
        "graphql",
        "-f",
        f"query={PR_FEEDBACK_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"repo={name}",
        "-F",
        f"number={number}",
    )
    return json.loads(out)["data"]["repository"]["pullRequest"]


def _post_json(args: list[str], body: str) -> int:
    result = subprocess.run(
        ["gh", "api", *args, "--method", "POST", "--input", "-"],
        input=json.dumps({"body": body}),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise GithubError(result.stderr.strip() or "gh failed")
    try:
        return int(json.loads(result.stdout).get("id", 0))
    except (ValueError, TypeError):
        return 0


def reply_to_review_comment(repo: str, number: int, comment_id: int, body: str) -> int:
    """Reply in a review thread. Returns the id of the new comment."""
    return _post_json([f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies"], body)


def comment_on_pr(repo: str, number: int, body: str) -> int:
    """A top-level PR comment. Returns the id of the new comment."""
    return _post_json([f"repos/{repo}/issues/{number}/comments"], body)


RESOLVE_THREAD = (
    "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
)


def resolve_thread(thread_id: str) -> None:
    """Mark a review thread resolved."""
    _gh("api", "graphql", "-f", f"query={RESOLVE_THREAD}", "-F", f"id={thread_id}")
