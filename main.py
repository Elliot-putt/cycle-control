"""Cycle Control: a Linear cycle dashboard that runs Claude Code builders in parallel."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import time

from aiohttp import web

from cyclecontrol.config import Settings
from cyclecontrol.server import App

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]


def serve(app: App, ready: threading.Event) -> None:
    async def run() -> None:
        runner = web.AppRunner(app.build())
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", app.settings.port).start()
        ready.set()
        await asyncio.Event().wait()

    asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-window", action="store_true", help="serve the dashboard without a window"
    )
    parser.add_argument(
        "--demo", action="store_true", help="use fake Linear data and fake builders"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    os.environ["PATH"] = ":".join([*os.environ.get("PATH", "").split(":"), *EXTRA_PATHS])
    settings = Settings(demo=args.demo or Settings().demo)
    if settings.demo:
        os.environ["CYCLE_NO_NOTIFY"] = "1"
    app = App(settings)
    ready = threading.Event()
    threading.Thread(target=serve, args=(app, ready), daemon=True, name="server").start()
    ready.wait(10)
    url = f"http://127.0.0.1:{settings.port}/"

    if args.no_window:
        print(f"Cycle Control: {url}")
        while True:
            time.sleep(3600)

    import webview

    webview.create_window("Cycle Control", url, width=1500, height=950, background_color="#0b0d12")
    webview.start()
    app.sessions.stop_all()


if __name__ == "__main__":
    main()
