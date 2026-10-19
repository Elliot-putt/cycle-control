"""Posts a PR title as a link to Slack as you, through the Slack connector of your Claude Code."""

from __future__ import annotations

import re
import subprocess
import tempfile

SEND_TOOL = "mcp__claude_ai_Slack__slack_send_message"
DRAFT_TOOL = "mcp__claude_ai_Slack__slack_send_message_draft"
LINK = re.compile(r"https://[^\s)>\]]*slack\.com[^\s)>\]]*")


class SlackError(RuntimeError):
    pass


def link_message(title: str, url: str) -> str:
    escaped = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{escaped}]({url})"


def prompt_for(channel_id: str, message: str, tool: str) -> str:
    name = tool.rsplit("__", 1)[-1]
    return (
        f'Call the {name} tool exactly once with channel_id "{channel_id}" and this message, character for character:\n'
        f"{message}\n"
        "Add nothing else. Then reply with only the link the tool returns."
    )


def post_pr_link(
    claude_bin: str, channel_id: str, title: str, url: str, draft: bool = False
) -> str:
    tool = DRAFT_TOOL if draft else SEND_TOOL
    argv = [
        claude_bin,
        "-p",
        "--model",
        "haiku",
        "--max-turns",
        "8",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--allowedTools",
        f"{tool},ToolSearch",
    ]
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            argv,
            input=prompt_for(channel_id, link_message(title, url), tool),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            cwd=cwd,
        )
    reply = result.stdout.strip()
    found = LINK.search(reply)
    if result.returncode != 0 or not found:
        raise SlackError(f"Slack post failed: {(reply or result.stderr).strip()[:200]}")
    return found.group(0)
