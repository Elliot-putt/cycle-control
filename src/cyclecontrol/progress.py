"""Reads the files the builder's scripts write into its worktree's git dir."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cyclecontrol.models import PlanStep, Progress

PR_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

FILES = ("spp-phase.json", "spp-plan.json", "spp-gates.json", "spp-review.json")


def progress_stamp(git_dir: Path) -> tuple[tuple[int, int], ...]:
    """Changes whenever one of the progress files does, so a reader can skip re-parsing them."""
    stamp: list[tuple[int, int]] = []
    for name in FILES:
        try:
            info = (git_dir / name).stat()
        except OSError:
            stamp.append((0, 0))
            continue
        stamp.append((info.st_mtime_ns, info.st_size))
    return tuple(stamp)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def read_progress(git_dir: Path) -> Progress:
    phase = _load(git_dir / "spp-phase.json") or {}
    plan = _load(git_dir / "spp-plan.json") or {}
    gates = _load(git_dir / "spp-gates.json")
    review = _load(git_dir / "spp-review.json")
    note = str(phase.get("note", ""))
    steps = [
        PlanStep(
            id=str(step.get("id", "")),
            label=str(step.get("label", "")),
            kind=str(step.get("kind", "")),
            files=_strings(step.get("files")),
            reuses=_strings(step.get("reuses")),
            new=_strings(step.get("new")),
        )
        for step in plan.get("steps", [])
        if isinstance(step, dict)
    ]
    return Progress(
        phase=str(phase.get("phase", "")) or "starting",
        note=note,
        summary=str(plan.get("summary", "")),
        plan=steps,
        gates="none" if gates is None else ("pass" if gates.get("ok") is True else "block"),
        review="none"
        if review is None
        else ("PASS" if review.get("verdict") == "PASS" else "CHANGES"),
        pr_url=" ".join(PR_LINK.findall(note)),
        gate_blocks=_strings((gates or {}).get("blocks")),
        gate_warns=_strings((gates or {}).get("warns")),
        review_findings=_strings((review or {}).get("findings")),
    )
