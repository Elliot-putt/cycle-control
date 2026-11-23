"""HTTP API, static UI and the terminal websocket, on one aiohttp app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import WSMsgType, web

from cyclecontrol.board import Board
from cyclecontrol.comments import CommentStore, merge_label_for
from cyclecontrol.commentwatch import QUEUE_POLL_SECONDS, CommentWatcher
from cyclecontrol.config import Settings, save_env_value
from cyclecontrol.family import FamilyService
from cyclecontrol.github import (
    GithubError,
    PullRequest,
    add_label,
    find_open_pr,
    find_prs_by_issue,
    get_pr,
    viewer_login,
)
from cyclecontrol.journal import Journal, install, note
from cyclecontrol.keepawake import KeepAwake
from cyclecontrol.locks import BuilderLockedError
from cyclecontrol.memwatch import CHECK_SECONDS as MEMORY_CHECK_SECONDS
from cyclecontrol.memwatch import MemoryWatchdog
from cyclecontrol.nuke import Nuker
from cyclecontrol.rebasewatch import RebaseWatcher
from cyclecontrol.sessions import (
    CI_GREEN_NOTE,
    NotYoursError,
    SessionManager,
    UnmappedTeamError,
    notify,
    screen_text,
)
from cyclecontrol.slack import SlackError, post_pr_link, posted_pr_links
from cyclecontrol.worktrees import WorktreeError

logger = logging.getLogger(__name__)

CI_NUDGE_AFTER_SECONDS = 600
MAX_CI_NUDGES = 2

ASSETS = Path(os.environ.get("CYCLE_ASSETS_DIR") or Path(__file__).parent / "ui" / "assets")
REFRESH_SECONDS = 120
FAMILY_REFRESH_SECONDS = 60


def assets_version() -> str:
    """Changes whenever a UI file does, so an open window can reload itself after an edit or an update."""
    try:
        return str(max(int(path.stat().st_mtime) for path in ASSETS.iterdir() if path.is_file()))
    except (OSError, ValueError):
        return ""


@web.middleware
async def no_cache_assets(request: web.Request, handler):
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


CLOCK_JUMP_SECONDS = 120


def thread_dump() -> list[dict[str, str]]:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    return [
        {"name": names.get(ident, str(ident)), "stack": "".join(traceback.format_stack(frame))}
        for ident, frame in sys._current_frames().items()
    ]


async def run_forever(
    name: str,
    step,
    seconds: float,
    delay_first: bool = False,
    after_jump: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Run a step again and again. An error is logged and the loop carries on, so one bad tick never silences the app.
    When the wall clock jumps past the pause (the Mac slept), after_jump runs first with the seconds away."""
    if delay_first:
        await asyncio.sleep(seconds)
    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background step %s failed; carrying on", name)
        before = clock()
        await asyncio.sleep(seconds)
        away = clock() - before - seconds
        if after_jump is not None and away > CLOCK_JUMP_SECONDS:
            try:
                await after_jump(away)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Catch-up after sleep failed; carrying on")


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.github_login and not settings.demo:
            settings.github_login = viewer_login()
        self.board = Board(settings)
        self.awake = KeepAwake(settings.keep_awake and not settings.demo)
        self.sessions = SessionManager(settings)
        self.family = FamilyService(settings, self.board, self.sessions)
        self.rebases = RebaseWatcher(settings, self.sessions)
        store = CommentStore(settings.comments_path)
        self.comments = CommentWatcher(settings, self.sessions, store)
        if not settings.demo:
            self.comments.find_all_by_issue = find_prs_by_issue
        self.nuker = Nuker(settings, self.sessions, self.board, store)
        self.memory = MemoryWatchdog(self.sessions, notify)
        self.journal = Journal(settings.journal_path)
        install(self.journal)

    def state(self) -> dict:
        waiting_after = self.settings.waiting_after_seconds
        sessions = [self._session_view(s, waiting_after) for s in self.sessions.all()]
        live = {s["id"] for s in sessions}
        queued = self.sessions.queued_ids()
        issues = [
            {
                "id": issue.id,
                "title": issue.title,
                "url": issue.url,
                "status": issue.status,
                "isOpen": issue.is_open,
                "isStarted": issue.is_started,
                "inCycle": issue.in_cycle,
                "isMine": issue.is_mine,
                "assignee": issue.assignee,
                "priority": issue.priority,
                "hasSession": issue.id in live,
                "isQueued": issue.id in queued,
                "parentId": issue.parent_id,
                "hasChildren": issue.has_children,
                "teamKey": issue.team,
                "blockedBy": [
                    b.id for b in issue.blocked_by if b.status_type not in ("completed", "canceled")
                ],
                **self._start_info(issue),
            }
            for issue in self.board.view()
        ]
        cycle = self.board.cycle
        return {
            "configured": self.board.is_configured,
            "demo": self.settings.demo,
            "error": self.board.error,
            "updatedAt": self.board.updated_at,
            "cycle": cycle.__dict__ if cycle else None,
            "issues": issues,
            "sessions": sessions,
            "groups": self.family.groups(),
            "queue": self.sessions.queue_view(),
            "maxBuilders": self.settings.max_builders,
            "slotsUsed": len(self.sessions.counted()),
            "liveBuilders": len(self.sessions.live()),
            "maxLiveBuilders": self.settings.max_live_builders,
            "queueErrors": self.sessions.errors,
            "nukes": self.nuker.view(),
            "journal": self.journal.recent(),
            "assets": assets_version(),
        }

    def _session_view(self, session, waiting_after: float) -> dict:
        view = session.summary(waiting_after)
        view["comments"] = self.comments.view(session)
        if view["comments"]["needsYou"] and view["outcome"] in ("working", "waiting"):
            view["outcome"] = "waiting"
        return view

    def _start_info(self, issue) -> dict:
        team = self.settings.team_for(issue.team)
        if team is None:
            return {
                "startable": False,
                "startReason": f"No repo mapped for team {issue.team}.",
                "hasWorktree": False,
            }
        return {
            "startable": True,
            "startReason": "",
            "hasWorktree": team.worktree_path(issue.id).exists(),
        }

    async def get_state(self, _: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(self.state))

    async def get_screen(self, request: web.Request) -> web.Response:
        """What the builder's terminal shows now, as the nudges read it: for checking why one did or did not fire."""
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        text = await asyncio.to_thread(lambda: screen_text(session.pty.tail()))
        return web.json_response(
            {
                "text": text,
                "alive": session.pty.is_alive(),
                "idleSeconds": int(time.time() - session.pty.last_output),
                "nudges": session.nudges,
                "nudgedAt": session.nudged_at,
                "ciNudgedAt": session.ci_nudged_at,
            }
        )

    async def get_threads(self, _: web.Request) -> web.Response:
        """Every thread's Python stack, for finding what holds the app up when it crawls. No sudo needed."""
        return web.json_response({"threads": thread_dump()})

    @property
    def _prefs_path(self):
        return self.settings.state_path.parent / "prefs.json"

    def _read_prefs(self) -> dict:
        try:
            data = json.loads(self._prefs_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    async def get_prefs(self, _: web.Request) -> web.Response:
        """The dashboard's own choices (Mine/Team, Plan/Auto, Open/Done), kept here so they survive a restart."""
        return web.json_response(self._read_prefs())

    async def post_prefs(self, request: web.Request) -> web.Response:
        body = await request.json()
        allowed = {
            "filter": {"mine", "team"},
            "startAs": {"plan", "auto"},
            "agentView": {"open", "done"},
        }
        prefs = self._read_prefs()
        for key, values in allowed.items():
            if body.get(key) in values:
                prefs[key] = body[key]
        self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._prefs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs))
        tmp.replace(self._prefs_path)
        return web.json_response(prefs)

    async def post_refresh(self, _: web.Request) -> web.Response:
        await asyncio.to_thread(self.board.refresh)
        return web.json_response(self.state())

    async def post_settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        limits = {"maxBuilders": "max_builders", "maxLiveBuilders": "max_live_builders"}
        for field, name in limits.items():
            if field not in body:
                continue
            try:
                value = int(body[field])
            except (TypeError, ValueError):
                return web.json_response({"error": f"{field} must be a number."}, status=400)
            if value < 1:
                return web.json_response({"error": f"{field} must be at least 1."}, status=400)
            setattr(self.settings, name, value)
            save_env_value(name.upper(), str(value))
        key = str(body.get("linearApiKey", "")).strip()
        if key:
            save_env_value("LINEAR_API_KEY", key)
            self.settings.linear_api_key = key
            await asyncio.to_thread(self.board.refresh)
        elif not any(field in body for field in limits):
            return web.json_response({"error": "Paste your Linear API key."}, status=400)
        return web.json_response(self.state())

    async def post_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        issue = self.board.find(str(body.get("issueId", "")))
        if issue is None:
            return web.json_response({"error": "Unknown issue."}, status=404)
        mode = "auto" if body.get("mode") == "auto" else "plan"
        resume = bool(body.get("resume"))
        if not resume:
            self.comments.forget_prs(issue.id)
        try:
            outcome = await asyncio.to_thread(
                self.sessions.request_start,
                issue,
                mode,
                str(body.get("base", "")),
                resume,
                force=bool(body.get("force")),
            )
        except NotYoursError as failure:
            return web.json_response({"error": str(failure), "notYours": True}, status=409)
        except UnmappedTeamError as failure:
            return web.json_response({"error": str(failure)}, status=400)
        except BuilderLockedError as failure:
            return web.json_response({"error": str(failure)}, status=409)
        except WorktreeError as failure:
            return web.json_response({"error": str(failure)}, status=500)
        except OSError as failure:
            return web.json_response({"error": f"Could not start claude: {failure}"}, status=500)
        if outcome == "started" and not resume:
            await asyncio.to_thread(self.board.mark_in_progress, issue.id)
        return web.json_response(self.state())

    async def post_start_ready(self, request: web.Request) -> web.Response:
        parent = next(
            (p for p in self.board.parents() if p.id == request.match_info["parent_id"]), None
        )
        if parent is None:
            return web.json_response({"error": "Unknown parent."}, status=404)
        body = await request.json()
        mode = "auto" if body.get("mode") == "auto" else "plan"
        await asyncio.to_thread(self.family.refresh, parent.id)
        by_id = {c.id: c for c in self.board.children_of(parent.id)}
        failures: list[str] = []
        for state in self.family.resolve(parent):
            if state.kind != "ready":
                continue
            try:
                outcome = await asyncio.to_thread(
                    self.sessions.request_start, by_id[state.issue_id], mode, state.base, False
                )
            except (NotYoursError, UnmappedTeamError, WorktreeError, OSError) as failure:
                failures.append(f"{state.issue_id}: {failure}")
                continue
            if outcome == "started":
                await asyncio.to_thread(self.board.mark_in_progress, state.issue_id)
        if failures:
            return web.json_response({"error": "; ".join(failures)}, status=500)
        return web.json_response(self.state())

    async def post_comments_poll(self, _: web.Request) -> web.Response:
        if self.settings.demo:
            for session in self.sessions.all():
                self.comments.seed_demo(session)
        else:
            await asyncio.to_thread(self.comments.poll)
            await asyncio.to_thread(self.comments.tick)
        return web.json_response(self.state())

    async def post_comment_reply(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        body = await request.json()
        text = str(body.get("body", "")).strip()
        if not text:
            return web.json_response({"error": "The reply is empty."}, status=400)
        try:
            if not self.settings.demo:
                await asyncio.to_thread(
                    self.comments.post_reply,
                    session,
                    request.match_info["key"],
                    text,
                    bool(body.get("resolve")),
                )
            else:
                self.comments.dismiss(session, request.match_info["key"])
        except GithubError as failure:
            return web.json_response({"error": str(failure)}, status=502)
        return web.json_response(self.state())

    async def post_comment_answer(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        body = await request.json()
        try:
            outcome = await asyncio.to_thread(
                self.comments.answer_builder,
                session,
                request.match_info["key"],
                str(body.get("text", "")),
            )
        except GithubError as failure:
            return web.json_response({"error": str(failure)}, status=400)
        except (BuilderLockedError, UnmappedTeamError, WorktreeError, OSError) as failure:
            return web.json_response(
                {"error": f"Could not wake the builder: {failure}"}, status=409
            )
        state = self.state()
        state["woken"] = outcome
        return web.json_response(state)

    async def post_comment_dismiss(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        self.comments.dismiss(session, request.match_info["key"])
        return web.json_response(self.state())

    async def post_clear_attention(self, request: web.Request) -> web.Response:
        self.sessions.clear_attention(request.match_info["issue_id"])
        return web.json_response(self.state())

    async def post_cancel_queued(self, request: web.Request) -> web.Response:
        self.sessions.cancel_queued(request.match_info["issue_id"])
        return web.json_response(self.state())

    async def post_stop(self, request: web.Request) -> web.Response:
        self.sessions.stop(request.match_info["issue_id"])
        return web.json_response(self.state())

    async def get_nuke_preview(self, request: web.Request) -> web.Response:
        issue = self.nuker.find_issue(request.match_info["issue_id"])
        if issue is None:
            return web.json_response({"error": "Unknown issue."}, status=404)
        return web.json_response(await asyncio.to_thread(self.nuker.preview, issue))

    async def post_nuke(self, request: web.Request) -> web.Response:
        issue = self.nuker.find_issue(request.match_info["issue_id"])
        if issue is None:
            return web.json_response({"error": "Unknown issue."}, status=404)
        body = await request.json() if request.can_read_body else {}
        unassign = bool(body.get("unassign", True)) if isinstance(body, dict) else True
        outcome = await asyncio.to_thread(self.nuker.start, issue, unassign)
        if outcome == "running":
            return web.json_response({"error": f"{issue.id} is already being nuked."}, status=409)
        return web.json_response(self.state(), status=202)

    async def post_nuke_dismiss(self, request: web.Request) -> web.Response:
        self.nuker.dismiss(request.match_info["issue_id"])
        return web.json_response(self.state())

    async def post_wake(self, request: web.Request) -> web.Response:
        try:
            outcome = await asyncio.to_thread(self.sessions.wake, request.match_info["issue_id"])
        except BuilderLockedError as failure:
            return web.json_response({"error": str(failure)}, status=409)
        except (UnmappedTeamError, WorktreeError, OSError) as failure:
            return web.json_response(
                {"error": f"Could not wake the builder: {failure}"}, status=500
            )
        if outcome == "unknown":
            return web.json_response({"error": "No session."}, status=404)
        return web.json_response(self.state())

    async def get_activity(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        data = await asyncio.to_thread(session.activity, self.settings.waiting_after_seconds)
        return web.json_response(data)

    async def post_approve(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        settings = self.settings
        try:
            if settings.demo:
                session.label_done, session.slack_link = True, "https://app.slack.com/demo-message"
            else:
                team = settings.team_for(session.issue.team)
                if team is None:
                    return web.json_response(
                        {"error": f"No repo mapped for team {session.issue.team}."}, status=400
                    )
                prs = await asyncio.to_thread(self._prs_to_approve, session, team.github_repo)
                for repo, pr in prs:
                    if pr.is_draft:
                        return web.json_response(
                            {"error": f"PR #{pr.number} is still a draft."}, status=400
                        )
                for repo, pr in prs:
                    await asyncio.to_thread(self._approve_pr, session, repo, pr)
        except (GithubError, SlackError) as failure:
            self.sessions.save()
            return web.json_response({"error": str(failure)}, status=502)
        session.approved = True
        self.sessions.save()
        return web.json_response(self.state())

    def nudge_green_ci(self, now: float | None = None) -> list:
        """A builder that marked `ci`, saw CI go green and then stopped (a question from Elliot, a lost
        notification) sits there with no Approve card. Tell it to finish the step, once per stall, and after
        MAX_CI_NUDGES stalls tell Elliot once instead of nudging forever (SLM-1516 took four)."""
        now = time.time() if now is None else now
        nudged = []
        for session in self.sessions.all():
            if not session.pty.is_alive() or session.stopped or session.finished:
                continue
            if session.progress().phase != "ci":
                session.ci_nudges, session.ci_escalated = 0, False
                continue
            if now - session.pty.last_output < CI_NUDGE_AFTER_SECONDS:
                continue
            if session.ci_nudged_at >= session.pty.last_output:
                continue
            view = self.comments.view(session)
            status = view.get("status") or {}
            if not (view.get("hasPr") and status.get("open") and status.get("ci") == "SUCCESS"):
                continue
            session.ci_nudged_at = now
            number = view.get("prNumber", "?")
            if session.ci_nudges >= MAX_CI_NUDGES:
                if not session.ci_escalated:
                    session.ci_escalated = True
                    notify(
                        f"{session.issue.id} needs you: CI is green but it will not hand over",
                        f"PR #{number} has been green through {session.ci_nudges} nudges. Have a look.",
                    )
                continue
            session.ci_nudges += 1
            session.send(text=CI_GREEN_NOTE.format(number=number))
            note(
                f"{session.issue.id} sat in ci with green checks",
                f"Told to hand the PR over (nudge {session.ci_nudges} of {MAX_CI_NUDGES}).",
            )
            nudged.append(session)
        return nudged

    def _prs_to_approve(self, session, fallback_repo: str) -> list[tuple[str, PullRequest]]:
        """Every PR the builder opened, oldest first, so a stack is labelled and posted as a whole.

        A link that does not resolve (a builder can mistype a repo name in its output) is skipped while another does."""
        linked = [
            match
            for url in session.pr_urls()
            if (match := re.match(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", url))
        ]
        if not linked:
            try:
                prs = [(fallback_repo, find_open_pr(fallback_repo, session.issue.branch))]
            except GithubError:
                # The PR may live in another repo; the comment store remembers its number and repo.
                store = self.comments._store
                known = store.numbers.get(session.issue.id)
                if not known:
                    raise
                repo = store.repos.get(session.issue.id) or fallback_repo
                prs = [(repo, get_pr(repo, known))]
        else:
            prs, failure = [], None
            for m in linked:
                try:
                    prs.append((m.group(1), get_pr(m.group(1), int(m.group(2)))))
                except GithubError as error:
                    failure = failure or error
            if not prs and failure:
                raise failure
        return App._with_stack_bases(prs, session.base)

    @staticmethod
    def _with_stack_bases(
        prs: list[tuple[str, PullRequest]], base: str
    ) -> list[tuple[str, PullRequest]]:
        """A stacked PR targets another open PR's branch. Walk down to the trunk so the builder's whole stack is
        approved, even when only the top PR was recorded (SLM-1428: only #1838 was known, #1837 was its base)."""
        trunk = {base, "develop", "main", "master"} - {""}
        for _ in range(6):
            repo, lowest = prs[0]
            if not lowest.base or lowest.base in trunk:
                break
            if any(pr.url.endswith(f"/{lowest.number}") for _, pr in prs[1:]):
                break
            try:
                below = find_open_pr(repo, lowest.base)
            except GithubError:
                break
            if any(pr.number == below.number for _, pr in prs):
                break
            prs.insert(0, (repo, below))
        return prs

    def _approve_pr(self, session, repo: str, pr: PullRequest) -> None:
        settings = self.settings
        if session.label_done and not session.labelled:
            session.labelled = [session.pr_url_from_chat()]
        if session.slack_link and not session.slack_links:
            session.slack_links = {session.pr_url_from_chat(): session.slack_link}
        if pr.url not in session.labelled:
            merge_label = merge_label_for(repo, settings.merge_label)
            if merge_label and merge_label not in pr.labels:
                add_label(repo, pr.number, merge_label)
            session.labelled.append(pr.url)
            session.label_done = True
            self.sessions.save()
        if pr.url not in session.slack_links:
            link = post_pr_link(settings.claude_bin, settings.slack_channel_id, pr.title, pr.url)
            session.slack_links[pr.url] = link
            session.slack_link = session.slack_link or link
            self.sessions.save()

    async def post_send(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        body = await request.json()
        text, key = str(body.get("text", "")), str(body.get("key", ""))
        if session.pty.is_alive():
            session.send(text=text, key=key)
            return web.json_response({"ok": True})
        try:
            outcome = await asyncio.to_thread(self.sessions.wake, session.issue.id, text.strip())
        except (BuilderLockedError, UnmappedTeamError, WorktreeError, OSError) as failure:
            return web.json_response(
                {"error": f"Could not wake the builder: {failure}"}, status=409
            )
        return web.json_response({"ok": True, "woken": outcome})

    async def terminal_ws(self, request: web.Request) -> web.StreamResponse:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.Response(status=404, text="No session.")
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def reader(data: bytes) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, data)

        for chunk in session.pty.attach(reader):
            await ws.send_bytes(chunk)

        async def pump() -> None:
            while True:
                await ws.send_bytes(await queue.get())

        pump_task = asyncio.create_task(pump())
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("type") == "input":
                    session.pty.write(str(payload.get("data", "")).encode())
                elif payload.get("type") == "resize":
                    session.pty.resize(int(payload["rows"]), int(payload["cols"]))
        finally:
            pump_task.cancel()
            session.pty.detach(reader)
        return ws

    async def index(self, _: web.Request) -> web.FileResponse:
        return web.FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-cache"})

    async def background(self, app: web.Application) -> None:
        async def refresh_step() -> None:
            await asyncio.to_thread(self.board.refresh)

        async def family_step() -> None:
            await asyncio.to_thread(self.family.refresh)

        async def rebase_step() -> None:
            for issue_id, action in await asyncio.to_thread(self.rebases.poll):
                logger.info("Rebase %s for %s onto %s", action.kind, issue_id, action.new_base)

        async def comments_step(everything: bool = False) -> None:
            woken = await asyncio.to_thread(self.comments.poll, not everything)
            if woken:
                logger.info("New review feedback for: %s", ", ".join(woken))

        async def watch_step() -> None:
            for session in await asyncio.to_thread(self.sessions.nudge_cut_off):
                logger.info("Nudged %s after a cut-off response", session.issue.id)
            for session in await asyncio.to_thread(self.nudge_green_ci):
                logger.info("Nudged %s: CI is green but it never handed over", session.issue.id)
            woken, stuck = await asyncio.to_thread(self.sessions.nudge_memory_kill)
            for session in woken:
                logger.info("Woke %s to retry after a low-memory kill", session.issue.id)
            for session in stuck:
                logger.info("%s was killed for memory again; told Elliot", session.issue.id)
            for session in await asyncio.to_thread(self.sessions.sleep_idle):
                logger.info("Put idle builder %s to sleep", session.issue.id)
            for issue_id in await asyncio.to_thread(self.sessions.forget_finished):
                logger.info("Forgot finished builder %s", issue_id)
            await asyncio.to_thread(self.sessions.drain)
            await asyncio.to_thread(self.sessions.release_dead)
            self.awake.update(len(self.sessions.counted()))
            if self.settings.auto_comments:
                await asyncio.to_thread(self.comments.tick)
            fresh, reminders = self.sessions.check_waiting()
            for session in fresh:
                notify(f"{session.issue.id} is waiting on you", session.issue.title)
            for session in reminders:
                notify(f"{session.issue.id} is still waiting on you", session.issue.title)

        async def catch_up_comments() -> None:
            await comments_step(everything=True)
            await asyncio.sleep(30)
            await comments_step(everything=True)

        async def back_from_sleep(away: float) -> None:
            """A builder mid-response when the Mac slept is cut off; tell it to carry on before anything else,
            and poll the PRs in a task of their own so the watch tick is never held up by slow network calls."""
            minutes = int(away // 60)
            note(f"Back after {minutes} min asleep", "Checking every PR now and again in 30 s.")
            logger.info("Clock jumped %s s: the Mac slept. Polling every PR now.", int(away))
            for session in await asyncio.to_thread(self.sessions.nudge_cut_off):
                logger.info("Nudged %s after a cut-off response, on waking", session.issue.id)
            if not self.settings.auto_comments or self.settings.demo:
                return
            tasks.append(asyncio.create_task(catch_up_comments()))

        async def memory_step() -> None:
            await asyncio.to_thread(self.memory.check)

        async def slack_step() -> None:
            """Elliot often posts a PR in the devs channel himself instead of pressing Approve."""
            if not self.sessions.unposted_pr_urls():
                return
            settings = self.settings
            try:
                found = await asyncio.to_thread(
                    posted_pr_links,
                    settings.claude_bin,
                    settings.slack_channel_id,
                    settings.slack_team_domain,
                )
            except (SlackError, OSError) as failure:
                logger.warning("Could not read the Slack channel: %s", failure)
                return
            for session, url, _link in await asyncio.to_thread(self.sessions.mark_posted, found):
                number = url.rsplit("/", 1)[-1]
                notify(
                    f"{session.issue.id}: you posted PR #{number} in Slack yourself",
                    "Noted. Approve now only adds the merge-queue label.",
                )

        async def restore_sessions() -> None:
            try:
                resumed = await asyncio.to_thread(self.sessions.restore)
            except Exception:
                logger.exception("Restoring builders failed")
                return
            if resumed:
                logger.info("Resumed builders: %s", ", ".join(resumed))

        async def after_restore(name: str, step, seconds: float) -> None:
            await restored
            await run_forever(name, step, seconds)

        restored = asyncio.create_task(restore_sessions())
        tasks = [
            restored,
            asyncio.create_task(run_forever("refresh", refresh_step, REFRESH_SECONDS)),
            asyncio.create_task(run_forever("family", family_step, FAMILY_REFRESH_SECONDS)),
            asyncio.create_task(run_forever("watch", watch_step, 2, after_jump=back_from_sleep)),
        ]
        if self.settings.auto_comments and not self.settings.demo:
            tasks.append(
                asyncio.create_task(after_restore("comments", comments_step, QUEUE_POLL_SECONDS))
            )
        if not self.settings.demo and sys.platform == "darwin":
            tasks.append(
                asyncio.create_task(run_forever("memory", memory_step, MEMORY_CHECK_SECONDS))
            )
        if self.settings.slack_scan_seconds > 0 and not self.settings.demo:
            tasks.append(
                asyncio.create_task(
                    after_restore("slack", slack_step, self.settings.slack_scan_seconds)
                )
            )
        if self.settings.auto_rebase:
            tasks.append(
                asyncio.create_task(
                    run_forever(
                        "rebase", rebase_step, self.settings.rebase_poll_seconds, delay_first=True
                    )
                )
            )
        yield
        for task in tasks:
            task.cancel()
        self.awake.stop()
        self.sessions.stop_all()

    def build(self) -> web.Application:
        app = web.Application(middlewares=[no_cache_assets])
        app.cleanup_ctx.append(self.background)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/api/state", self.get_state),
                web.get("/api/prefs", self.get_prefs),
                web.post("/api/prefs", self.post_prefs),
                web.post("/api/refresh", self.post_refresh),
                web.post("/api/settings", self.post_settings),
                web.post("/api/start", self.post_start),
                web.post("/api/sessions/{issue_id}/stop", self.post_stop),
                web.post("/api/sessions/{issue_id}/wake", self.post_wake),
                web.get("/api/sessions/{issue_id}/nuke-preview", self.get_nuke_preview),
                web.post("/api/sessions/{issue_id}/nuke", self.post_nuke),
                web.post("/api/nukes/{issue_id}/dismiss", self.post_nuke_dismiss),
                web.post("/api/queue/{issue_id}/cancel", self.post_cancel_queued),
                web.post("/api/sessions/{issue_id}/clear-attention", self.post_clear_attention),
                web.post("/api/comments/poll", self.post_comments_poll),
                web.post("/api/sessions/{issue_id}/comments/{key}/reply", self.post_comment_reply),
                web.post(
                    "/api/sessions/{issue_id}/comments/{key}/dismiss", self.post_comment_dismiss
                ),
                web.post(
                    "/api/sessions/{issue_id}/comments/{key}/answer", self.post_comment_answer
                ),
                web.post("/api/groups/{parent_id}/start-ready", self.post_start_ready),
                web.get("/api/sessions/{issue_id}/activity", self.get_activity),
                web.get("/api/sessions/{issue_id}/screen", self.get_screen),
                web.get("/api/debug/threads", self.get_threads),
                web.post("/api/sessions/{issue_id}/send", self.post_send),
                web.post("/api/sessions/{issue_id}/approve", self.post_approve),
                web.get("/ws/terminal/{issue_id}", self.terminal_ws),
                web.static("/static", ASSETS, follow_symlinks=False),
            ]
        )
        return app
