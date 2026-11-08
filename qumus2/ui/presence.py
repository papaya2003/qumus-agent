"""Stop the server once no Qumus page is open (Jialei 2026-11-06).

"I want it to stop by itself when I close the window in the browser." Every
open page keeps the /ws/events socket open and polls /api/health every 5 s, so
"no page open" is: no socket open and no page request for `idle_s` seconds.
The wait lets a reload or an accidentally closed tab come back. While the
agents or a lab job work, the server stays up (an overnight run is not cut off
by closing a tab); it stops once that work ends and still no page is open.

Stopping sends this process SIGINT, the same path as Control+C: agents pause at
a safe point, the lab process saves its state, the database closes cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def interrupt_self() -> None:
    os.kill(os.getpid(), signal.SIGINT)


class IdleStop:
    def __init__(self, idle_s: float, working: Callable[[], bool], *,
                 stop: Callable[[], None] = interrupt_self, first_page_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.idle_s = idle_s
        # Until a first page arrives (the browser may be slow to open), wait longer.
        self.first_page_s = max(first_page_s, idle_s)
        self.working = working
        self.stop = stop
        self.clock = clock
        self.open = 0
        self.seen = False
        self.last = clock()
        self.stopped = False

    def opened(self) -> None:
        self.open += 1
        self.touched()

    def closed(self) -> None:
        self.open = max(0, self.open - 1)
        self.last = self.clock()

    def touched(self) -> None:
        self.seen = True
        self.last = self.clock()

    def due(self) -> bool:
        if self.stopped or self.open:
            return False
        if self.clock() - self.last < (self.idle_s if self.seen else self.first_page_s):
            return False
        try:
            return not self.working()
        except Exception:   # noqa: BLE001 - an unreadable state must not stop the lab
            log.exception('could not tell whether Qumus is working; staying up')
            return False

    def check(self) -> bool:
        if not self.due():
            return False
        self.stopped = True
        log.warning('No Qumus page has been open for %.0f s and nothing is running: '
                    'stopping (the same as Control+C). Start Qumus again to continue.', self.idle_s)
        self.stop()
        return True

    async def run_forever(self, interval_s: float = 5.0) -> None:
        while not self.check():
            await asyncio.sleep(interval_s)
