"""Keeps the Mac from idle-sleeping while builders are running.

A sleeping Mac cut builders off mid-response and stalled their commands for 15 minutes or more
(SLM-1063, SLM-1271, SLM-1275). `caffeinate -i` holds only the idle sleep off; closing the lid still sleeps.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


class KeepAwake:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled and shutil.which("caffeinate") is not None
        self._proc: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def update(self, live_builders: int) -> None:
        """Hold the Mac awake while at least one builder is running, and let go when none is."""
        if not self._enabled:
            return
        if live_builders > 0 and not self.active:
            self._proc = subprocess.Popen(
                ["caffeinate", "-i"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            logger.info("Keeping the Mac awake while builders run")
        elif live_builders == 0 and self.active:
            self.stop()

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            logger.info("Let the Mac sleep again")
        self._proc = None
