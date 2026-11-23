"""Posts a PR title as a link to Slack as you, through the Slack connector of your Claude Code."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile

SEND_TOOL = "mcp__claude_ai_Slack__slack_send_message"
DRAFT_TOOL = "mcp__claude_ai_Slack__slack_send_message_draft"
READ_TOOL = "mcp__claude_ai_Slack__slack_read_channel"
# A background `claude -p` run can see a Slack plugin's server instead of the claude.ai connector, so the same tool
# is also allowed under the names the `slack` plugin and the org-synced `engineering` plugin give it.
PLUGIN_PREFIXES = ("mcp__plugin_slack_slack__", "mcp__plugin_engineering_slack__")
LINK = re.compile(r"https://[^\s)>\]]*slack\.com[^\s)>\]]*")
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def allowed_names(tool: str) -> str:
    short = tool.rsplit("__", 1)[-1]
    return ",".join([tool, *(prefix + short for prefix in PLUGIN_PREFIXES)])


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
        f"{allowed_names(tool)},ToolSearch",
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


def read_prompt(channel_id: str, limit: int) -> str:
    return (
        f'Call the slack_read_channel tool exactly once for channel_id "{channel_id}", the most recent {limit} messages. '
        "Then reply with only a JSON array, no prose and no code fence: one object per GitHub pull request URL found "
        'in a message, with keys "pr" (the URL) and "ts" (that message\'s ts). A message with several PR URLs gives '
        "several objects. An empty array when there are none."
    )


def permalink(team_domain: str, channel_id: str, ts: str) -> str:
    return f"https://{team_domain}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def parse_posted(reply: str, team_domain: str, channel_id: str) -> dict[str, str]:
    """PR URL -> permalink of the newest channel message that carries it."""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end < start:
        raise SlackError(f"Slack read gave no list: {reply.strip()[:200]}")
    try:
        items = json.loads(reply[start : end + 1])
    except ValueError as bad:
        raise SlackError(f"Slack read gave bad JSON: {reply.strip()[:200]}") from bad
    found: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        url = PR_URL.match(str(item.get("pr", "")))
        ts = str(item.get("ts", ""))
        if url and ts and url.group(0) not in found:
            found[url.group(0)] = permalink(team_domain, channel_id, ts)
    return found


def posted_pr_links(
    claude_bin: str, channel_id: str, team_domain: str, limit: int = 60
) -> dict[str, str]:
    """Every PR link in the channel's recent messages, whoever posted it, with the message it sits in."""
    argv = [
        claude_bin,
        "-p",
        "--model",
        "haiku",
        "--max-turns",
        "6",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--allowedTools",
        f"{allowed_names(READ_TOOL)},ToolSearch",
    ]
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            argv,
            input=read_prompt(channel_id, limit),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            cwd=cwd,
        )
    if result.returncode != 0:
        raise SlackError(f"Slack read failed: {(result.stdout or result.stderr).strip()[:200]}")
    return parse_posted(result.stdout, team_domain, channel_id)
