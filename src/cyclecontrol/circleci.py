"""CircleCI: read why a job failed and re-run a workflow from its failed jobs. Inert without CIRCLECI_TOKEN."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

API = "https://circleci.com/api"
WORKFLOW_ID = re.compile(r"/workflow/([0-9a-f-]{36})")
TAIL = 1200

INFRA_SIGNS = (
    "shallow file has changed since we read it",
    "could not resolve host",
    "connection timed out",
    "connection reset by peer",
    "network is unreachable",
    "unexpected disconnect while reading sideband packet",
    "early eof",
    "rpc failed",
    "too many requests",
    "503 service unavailable",
    "502 bad gateway",
    "504 gateway",
    "no space left on device",
    "received unexpected status code 5",
    "failed to connect to",
    "temporary failure in name resolution",
)
INFRA_STEPS = (
    "checkout",
    "fetching",
    "fetch",
    "restor",
    "install",
    "spin up",
    "preparing environment",
)


@dataclass(frozen=True)
class Failure:
    workflow_id: str
    job: str
    step: str
    tail: str

    @property
    def infra(self) -> bool:
        """A failure in getting the code or the environment, not in the code: worth a re-run before a builder."""
        text = self.tail.lower()
        if any(sign in text for sign in INFRA_SIGNS):
            return True
        return any(word in self.step.lower() for word in INFRA_STEPS) and "fatal:" in text

    @property
    def line(self) -> str:
        lines = [ln.strip() for ln in self.tail.splitlines() if ln.strip()]
        return next(
            (ln for ln in reversed(lines) if not ln.lower().startswith("exited with")),
            lines[-1] if lines else "",
        )


def workflow_id(url: str) -> str:
    found = WORKFLOW_ID.search(url or "")
    return found.group(1) if found else ""


class CircleCI:
    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        self._token = os.environ.get("CIRCLECI_TOKEN", "") if token is None else token
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _get(self, path: str) -> dict | list:
        response = httpx.get(
            f"{API}/{path}", headers={"Circle-Token": self._token}, timeout=self._timeout
        )
        response.raise_for_status()
        return response.json()

    def failure(self, repo: str, check_url: str) -> Failure | None:
        """The failed step and the tail of its output for one red check, or None when it cannot be read."""
        wf = workflow_id(check_url)
        if not wf or not self.enabled:
            return None
        try:
            jobs = self._get(f"v2/workflow/{wf}/job")["items"]
            failed = [j for j in jobs if j.get("status") == "failed" and j.get("job_number")]
            if not failed:
                return None
            job = failed[0]
            detail = self._get(f"v1.1/project/gh/{repo}/{job['job_number']}")
            for step in detail.get("steps", []):
                for action in step.get("actions", []):
                    if not (action.get("failed") or action.get("status") == "failed"):
                        continue
                    tail = ""
                    if action.get("output_url"):
                        out = httpx.get(action["output_url"], timeout=self._timeout).json()
                        tail = "".join(o.get("message", "") for o in out)[-TAIL:]
                    return Failure(wf, str(job.get("name", "")), str(step.get("name", "")), tail)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as failure:
            logger.warning("Could not read the CircleCI failure behind %s: %s", check_url, failure)
        return None

    def rerun_from_failed(self, wf: str) -> bool:
        if not wf or not self.enabled:
            return False
        try:
            response = httpx.post(
                f"{API}/v2/workflow/{wf}/rerun",
                headers={"Circle-Token": self._token},
                json={"from_failed": True},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as failure:
            logger.warning("Could not re-run CircleCI workflow %s: %s", wf, failure)
            return False
        return True
