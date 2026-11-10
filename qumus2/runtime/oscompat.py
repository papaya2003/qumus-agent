"""The few process operations that differ between macOS/Linux and Windows.

Windows has no SIGKILL, and `os.kill(pid, sig)` there ends the process at once
(TerminateProcess) for every signal except the console events, so a "polite"
SIGINT/SIGTERM sent from outside cannot be caught. Callers use these helpers
instead of calling os.kill with a POSIX-only signal.
"""

from __future__ import annotations

import os
import signal

WINDOWS = os.name == "nt"


def force_kill(pid: int) -> None:
    """End `pid` without letting it clean up (kill -9; TerminateProcess on Windows).
    Raises OSError like os.kill when the process is gone."""
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def interrupt_self() -> None:
    """Deliver SIGINT to this process, the same path as Control+C.

    On Windows `os.kill(os.getpid(), SIGINT)` would terminate the process
    without running the handler, so the signal is raised in-process instead."""
    if WINDOWS:
        signal.raise_signal(signal.SIGINT)
    else:
        os.kill(os.getpid(), signal.SIGINT)


def stop_signals() -> tuple[signal.Signals, ...]:
    """Signals that ask a process to stop cleanly on this platform
    (Windows adds Control+Break)."""
    extra = (signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ()
    return (signal.SIGTERM, signal.SIGINT, *extra)


def pid_alive(pid: int) -> bool:
    """Whether process `pid` still runs, without touching it.

    POSIX: signal 0. Windows: OpenProcess + GetExitCodeProcess, because
    os.kill(pid, 0) there would terminate the process."""
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    process_query_limited_information, still_active, access_denied = 0x1000, 259, 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied   # exists, owned by someone else
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
