"""HTTP API, static UI and the terminal websocket, on one aiohttp app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import WSMsgType, web

from cyclecontrol.board import Board
from cyclecontrol.comments import CommentStore
from cyclecontrol.commentwatch import QUEUE_POLL_SECONDS, CommentWatcher
from cyclecontrol.config import Settings, save_env_value
from cyclecontrol.family import FamilyService
from cyclecontrol.github import GithubError, add_label, find_open_pr
from cyclecontrol.locks import BuilderLockedError
from cyclecontrol.rebasewatch import RebaseWatcher
from cyclecontrol.sessions import SessionManager, UnmappedTeamError, notify
from cyclecontrol.slack import SlackError, post_pr_link
from cyclecontrol.worktrees import WorktreeError

logger = logging.getLogger(__name__)

ASSETS = Path(os.environ.get("CYCLE_ASSETS_DIR") or Path(__file__).parent / "ui" / "assets")
REFRESH_SECONDS = 120
FAMILY_REFRESH_SECONDS = 60


@web.middleware
async def no_cache_assets(request: web.Request, handler):
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


async def run_forever(name: str, step, seconds: float, delay_first: bool = False) -> None:
    """Run a step again and again. An error is logged and the loop carries on, so one bad tick never silences the app."""
    if delay_first:
        await asyncio.sleep(seconds)
    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background step %s failed; carrying on", name)
        await asyncio.sleep(seconds)


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.board = Board(settings)
        self.sessions = SessionManager(settings)
        self.family = FamilyService(settings, self.board, self.sessions)
        self.rebases = RebaseWatcher(settings, self.sessions)
        self.comments = CommentWatcher(
            settings, self.sessions, CommentStore(settings.comments_path)
        )

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
            "queueErrors": self.sessions.errors,
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
        return web.json_response(self.state())

    async def post_refresh(self, _: web.Request) -> web.Response:
        await asyncio.to_thread(self.board.refresh)
        return web.json_response(self.state())

    async def post_settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        key = str(body.get("linearApiKey", "")).strip()
        if not key:
            return web.json_response({"error": "Paste your Linear API key."}, status=400)
        save_env_value("LINEAR_API_KEY", key)
        self.settings.linear_api_key = key
        await asyncio.to_thread(self.board.refresh)
        return web.json_response(self.state())

    async def post_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        issue = self.board.find(str(body.get("issueId", "")))
        if issue is None:
            return web.json_response({"error": "Unknown issue."}, status=404)
        mode = "auto" if body.get("mode") == "auto" else "plan"
        resume = bool(body.get("resume"))
        try:
            outcome = await asyncio.to_thread(
                self.sessions.request_start, issue, mode, str(body.get("base", "")), resume
            )
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
        await asyncio.to_thread(self.family.refresh)
        by_id = {c.id: c for c in self.board.children_of(parent.id)}
        failures: list[str] = []
        for state in self.family.resolve(parent):
            if state.kind != "ready":
                continue
            try:
                outcome = await asyncio.to_thread(
                    self.sessions.request_start, by_id[state.issue_id], mode, state.base, False
                )
            except (UnmappedTeamError, WorktreeError, OSError) as failure:
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
                pr = await asyncio.to_thread(find_open_pr, team.github_repo, session.issue.branch)
                if pr.is_draft:
                    return web.json_response({"error": "The PR is still a draft."}, status=400)
                if not session.label_done:
                    if settings.merge_label not in pr.labels:
                        await asyncio.to_thread(
                            add_label, team.github_repo, pr.number, settings.merge_label
                        )
                    session.label_done = True
                    self.sessions.save()
                if not session.slack_link:
                    session.slack_link = await asyncio.to_thread(
                        post_pr_link,
                        settings.claude_bin,
                        settings.slack_channel_id,
                        pr.title,
                        pr.url,
                    )
        except (GithubError, SlackError) as failure:
            self.sessions.save()
            return web.json_response({"error": str(failure)}, status=502)
        session.approved = True
        self.sessions.save()
        return web.json_response(self.state())

    async def post_send(self, request: web.Request) -> web.Response:
        session = self.sessions.get(request.match_info["issue_id"])
        if session is None:
            return web.json_response({"error": "No session."}, status=404)
        body = await request.json()
        session.send(text=str(body.get("text", "")), key=str(body.get("key", "")))
        return web.json_response({"ok": True})

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

        async def comments_step() -> None:
            woken = await asyncio.to_thread(self.comments.poll, True)
            if woken:
                logger.info("New review feedback for: %s", ", ".join(woken))

        async def watch_step() -> None:
            await asyncio.to_thread(self.sessions.drain)
            await asyncio.to_thread(self.sessions.release_dead)
            if self.settings.auto_comments:
                await asyncio.to_thread(self.comments.tick)
            fresh, reminders = self.sessions.check_waiting()
            for session in fresh:
                notify(f"{session.issue.id} is waiting on you", session.issue.title)
            for session in reminders:
                notify(f"{session.issue.id} is still waiting on you", session.issue.title)

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
            asyncio.create_task(run_forever("watch", watch_step, 2)),
        ]
        if self.settings.auto_comments and not self.settings.demo:
            tasks.append(
                asyncio.create_task(after_restore("comments", comments_step, QUEUE_POLL_SECONDS))
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
        self.sessions.stop_all()

    def build(self) -> web.Application:
        app = web.Application(middlewares=[no_cache_assets])
        app.cleanup_ctx.append(self.background)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/api/state", self.get_state),
                web.post("/api/refresh", self.post_refresh),
                web.post("/api/settings", self.post_settings),
                web.post("/api/start", self.post_start),
                web.post("/api/sessions/{issue_id}/stop", self.post_stop),
                web.post("/api/queue/{issue_id}/cancel", self.post_cancel_queued),
                web.post("/api/sessions/{issue_id}/clear-attention", self.post_clear_attention),
                web.post("/api/comments/poll", self.post_comments_poll),
                web.post("/api/sessions/{issue_id}/comments/{key}/reply", self.post_comment_reply),
                web.post(
                    "/api/sessions/{issue_id}/comments/{key}/dismiss", self.post_comment_dismiss
                ),
                web.post("/api/groups/{parent_id}/start-ready", self.post_start_ready),
                web.get("/api/sessions/{issue_id}/activity", self.get_activity),
                web.post("/api/sessions/{issue_id}/send", self.post_send),
                web.post("/api/sessions/{issue_id}/approve", self.post_approve),
                web.get("/ws/terminal/{issue_id}", self.terminal_ws),
                web.static("/static", ASSETS, follow_symlinks=False),
            ]
        )
        return app
