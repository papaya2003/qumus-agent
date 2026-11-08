"""Minimal local RPC between the app process and the lab process.

stdlib `multiprocessing.connection` on 127.0.0.1 with an auth key (HMAC
challenge), one short connection per call so any thread may call. Only the
methods registered on the server can be called. Errors come back as the same
exception class for ValueError / KeyError / TimeoutError, else RuntimeError.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from multiprocessing.connection import Client, Listener
from typing import Any

_ERRORS: dict[str, type[Exception]] = {"ValueError": ValueError, "KeyError": KeyError,
                                       "TimeoutError": TimeoutError}


class RpcServer:
    def __init__(self, methods: dict[str, Callable[..., Any]], authkey: bytes,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.methods = methods
        self._listener = Listener((host, port), authkey=authkey)
        self.address: tuple[str, int] = self._listener.address  # type: ignore[assignment]
        self._closed = False

    def serve_forever(self) -> None:
        while not self._closed:
            try:
                conn = self._listener.accept()
            except OSError:
                if self._closed:
                    return
                continue  # failed handshake (wrong key) or a dropped client
            threading.Thread(target=self._handle, args=(conn,), daemon=True,
                             name="rpc-call").start()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True, name="rpc-server")
        t.start()
        return t

    def _handle(self, conn: Any) -> None:
        with conn:
            try:
                name, args, kwargs = conn.recv()
                fn = self.methods.get(name)
                if fn is None:
                    conn.send(("err", "KeyError", f"no RPC method {name!r}"))
                    return
                conn.send(("ok", fn(*args, **kwargs)))
            except EOFError:
                return
            except Exception as e:  # noqa: BLE001 - report to the caller, keep serving
                conn.send(("err", type(e).__name__, str(e.args[0]) if
                           isinstance(e, KeyError) and e.args else str(e)))

    def close(self) -> None:
        self._closed = True
        self._listener.close()


class RpcClient:
    def __init__(self, address: tuple[str, int], authkey: bytes) -> None:
        self.address, self.authkey = address, authkey

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Raises ConnectionError when the lab process is not reachable."""
        try:
            conn = Client(self.address, authkey=self.authkey)
        except OSError as e:
            raise ConnectionError(f"lab process not reachable at {self.address}: {e}") from e
        with conn:
            try:
                conn.send((name, args, kwargs))
                reply = conn.recv()
            except (EOFError, OSError) as e:
                raise ConnectionError(f"lab process dropped the call {name}: {e}") from e
        if reply[0] == "ok":
            return reply[1]
        raise _ERRORS.get(reply[1], RuntimeError)(reply[2])
