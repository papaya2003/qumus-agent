"""Run an asyncio coroutine that spawns subprocesses (the Claude Agent SDK) without
the Windows-proactor cleanup noise.

On Windows, `asyncio.run(...)` closes the event loop while the SDK's `claude`
subprocess transport is still finalizing. When that transport is later garbage
collected, its `__del__` calls `repr()` on an already-closed pipe and raises
`ValueError: I/O operation on closed pipe`, which Python prints as an unraisable
exception / ResourceWarning. It is harmless but spams the console and the SSE
stream. This module:
  1. drains pending tasks + async generators and gives the loop a tick so the
     subprocess-exit callbacks close their pipes before the loop is closed, and
  2. installs a `sys.unraisablehook` / warnings filter that swallows exactly this
     asyncio-internal noise (nothing else).
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from typing import Any, Coroutine

_HOOK_INSTALLED = False

_NOISE_SIGNS = (
    "I/O operation on closed pipe",
    "unclosed transport",
    "unclosed <",
    "Event loop is closed",
    "BaseSubprocessTransport.__del__",
    "_ProactorBasePipeTransport.__del__",
)


def _install_noise_filter() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    _HOOK_INSTALLED = True

    warnings.filterwarnings("ignore", category=ResourceWarning)

    prev_hook = getattr(sys, "unraisablehook", None)

    def _hook(unraisable):  # pragma: no cover - only fires during GC on Windows
        exc = getattr(unraisable, "exc_value", None)
        obj_repr = ""
        try:
            obj_repr = repr(getattr(unraisable, "object", ""))
        except Exception:
            pass
        text = f"{exc!r} {obj_repr}"
        if any(sign in text for sign in _NOISE_SIGNS):
            return  # swallow the asyncio proactor cleanup noise
        if callable(prev_hook):
            prev_hook(unraisable)

    sys.unraisablehook = _hook


def run_sdk_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    """Like asyncio.run(coro) but tidies up subprocess transports on Windows."""
    _install_noise_filter()

    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            # let proactor subprocess-exit callbacks close their pipes
            loop.run_until_complete(asyncio.sleep(0.25))
        except Exception:
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)
