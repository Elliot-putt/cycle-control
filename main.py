"""Cycle Control: a Linear cycle dashboard that runs Claude Code builders in parallel."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web

from cyclecontrol.config import Settings
from cyclecontrol.server import App

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]


def serve(app: App, ready: threading.Event) -> None:
    async def run() -> None:
        runner = web.AppRunner(app.build(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", app.settings.port).start()
        ready.set()
        await asyncio.Event().wait()

    asyncio.run(run())


def configure_logging(path: Path) -> None:
    """The terminal and a rotating file, so what the app did while you were away can be read back."""
    fmt = logging.Formatter("%(asctime)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3))
    except OSError:
        pass
    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-window", action="store_true", help="serve the dashboard without a window"
    )
    parser.add_argument(
        "--demo", action="store_true", help="use fake Linear data and fake builders"
    )
    args = parser.parse_args()

    os.environ["PATH"] = ":".join([*os.environ.get("PATH", "").split(":"), *EXTRA_PATHS])
    settings = Settings(demo=args.demo or Settings().demo)
    url = f"http://127.0.0.1:{settings.port}/"
    if not args.no_window and server_is_up(url):
        open_window(url, settings)
        return
    configure_logging(settings.log_path)
    if settings.demo:
        os.environ["CYCLE_NO_NOTIFY"] = "1"
    app = App(settings)
    ready = threading.Event()
    threading.Thread(target=serve, args=(app, ready), daemon=True, name="server").start()
    ready.wait(10)

    if args.no_window:
        print(f"Cycle Control: {url}")
        while True:
            time.sleep(3600)

    open_window(url, settings)
    try:
        app.sessions.stop_all()
    finally:
        logging.shutdown()
        os._exit(0)


def server_is_up(url: str) -> bool:
    """A background Cycle Control (the launchd agent) already serves the dashboard: only a window is needed."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}api/state", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def open_window(url: str, settings: Settings) -> None:
    import webview

    webview.create_window("Cycle Control", url, width=1500, height=950, background_color="#0b0d12")
    webview.start(private_mode=False, storage_path=str(settings.state_path.parent / "webview"))


if __name__ == "__main__":
    main()
