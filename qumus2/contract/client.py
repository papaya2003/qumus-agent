"""HTTP client for instrument service contract v1.

One ``ContractClient`` talks to one service (a Qumus lab module such as
``exfoliation``/``stacking``, or one reactor service such as ``furnace``).
Standard library only, so it runs on the Mac, the Windows lab PC and Linux.

Rules this client keeps (contract v1, ADR 0001 of the reactor repo, and the
Qumus rules in notes/night-20261108/contract-v1-qumus.md):

* Reads (describe, state, events) may be repeated freely.
* ``command`` is sent exactly once. No reply -> ``OutcomeUnknown``; the caller
  must read the state, never resend motion.
* ``stop`` needs no lease and is idempotent, so it is retried on transport errors.
* A ``LeaseKeeper`` thread keeps the single control lease. If a renewal fails it
  reports the loss and stops; it never takes the lease back by itself.
* There is no ``unlock`` method on purpose: clearing ``locked`` is a person's
  decision at the lab PC (Qumus rule) or at the reactor host.
* No redirects are followed and environment proxies are ignored by default,
  because the services are loopback/VPN only.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .errors import (
    BadRequest,
    CommandFailed,
    CommandTimeout,
    ContractError,
    ContractVersionError,
    Forbidden,
    LeaseLost,
    OutcomeUnknown,
    TransportError,
    error_for_status,
)
from .models import (
    CommandReceipt,
    CommandSpec,
    Event,
    EventPage,
    LastCommand,
    ServiceDescription,
    ServiceState,
    StopResult,
)

CONTRACT_VERSION = 1
DEFAULT_CLIENT_HEADER = "X-Qumus-Client"

#: A guard sees every command before it is sent and raises to refuse it.
CommandGuard = Callable[[str, dict[str, Any], CommandSpec], None]


_log = logging.getLogger(__name__)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, f"redirect refused: {newurl}",
                                     headers, fp)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_client_id(client_id: str) -> str:
    if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
        raise ValueError("client_id must be 1 to 128 printable ASCII characters")
    if any(not " " <= ch <= "~" for ch in client_id):
        raise ValueError("client_id must be 1 to 128 printable ASCII characters")
    return client_id


class ContractClient:
    """Client for one contract v1 service.

    ``base_url`` includes the service base path, e.g. ``http://127.0.0.1:8781/``
    or, for the reactor stage, ``http://127.0.0.1:8767/contract/``.

    ``client_header``: header that carries ``client_id``. None means: use
    ``describe.client_header`` if the service names one, else ``X-Qumus-Client``.
    ``extra_headers`` are sent on every POST (the reactor robot needs
    ``X-MG400-Panel: 1``).

    ``body_style``: ``"flat"`` sends ``{"action": a, **params}`` (ADR 0001),
    ``"nested"`` sends ``{"action": a, "params": {...}}`` (Qumus spec-file
    wording). ``"auto"`` picks nested only when describe lists ``actions``.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        *,
        client_header: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_s: float = 5.0,
        command_timeout_s: float = 10.0,
        body_style: str = "auto",
        use_env_proxy: bool = False,
        guard: CommandGuard | None = None,
        name: str | None = None,
    ) -> None:
        if body_style not in {"auto", "flat", "nested"}:
            raise ValueError("body_style must be auto, flat or nested")
        if not base_url.endswith("/"):
            base_url += "/"
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"base_url must be an http(s) URL with a host: {base_url!r}")
        self.base_url = base_url
        self.host = parsed.hostname
        self.client_id = validate_client_id(client_id)
        self._client_header = client_header
        self.extra_headers = dict(extra_headers or {})
        self.timeout_s = timeout_s
        self.command_timeout_s = command_timeout_s
        self.body_style = body_style
        self.guard = guard
        self.name = name or base_url
        handlers: list[Any] = [_NoRedirect()]
        if not use_env_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)
        self._description: ServiceDescription | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ HTTP
    @property
    def client_header(self) -> str:
        if self._client_header:
            return self._client_header
        if self._description is not None and self._description.client_header:
            return self._description.client_header
        return DEFAULT_CLIENT_HEADER

    def url(self, path: str) -> str:
        return urllib.parse.urljoin(self.base_url, path.lstrip("/"))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 *, timeout_s: float | None = None, unknown_outcome: bool = False) -> Any:
        url = self.url(path)
        headers = {"Accept": "application/json"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers[self.client_header] = self.client_id
            headers.update(self.extra_headers)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout_s or self.timeout_s) as reply:
                raw = reply.read()
        except urllib.error.HTTPError as exc:
            message = exc.reason if isinstance(exc.reason, str) else str(exc)
            try:
                payload = json.loads(exc.read() or b"{}")
                if isinstance(payload, dict) and payload.get("error"):
                    message = str(payload["error"])
            except (ValueError, OSError):
                pass
            raise error_for_status(exc.code, message, url) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError) or (
                isinstance(reason, OSError) and reason.errno in (61, 111, 10061)
            ):
                # Nothing reached the service, so even a command is known not to have run.
                raise TransportError(f"connection refused: {reason}", url=url) from None
            cls = OutcomeUnknown if unknown_outcome else TransportError
            raise cls(f"no reply: {reason}", url=url) from None
        except (TimeoutError, http.client.HTTPException, ConnectionError) as exc:
            cls = OutcomeUnknown if unknown_outcome else TransportError
            raise cls(f"no reply: {exc!r}", url=url) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            cls = OutcomeUnknown if unknown_outcome else TransportError
            raise cls("reply was not JSON", url=url) from None

    # ----------------------------------------------------------------- reads
    def describe(self, *, refresh: bool = False) -> ServiceDescription:
        """GET api/describe. Refuses services that are not contract 1."""
        with self._lock:
            if self._description is not None and not refresh:
                return self._description
        raw = self._request("GET", "api/describe")
        if not isinstance(raw, dict):
            raise TransportError("describe reply was not an object", url=self.url("api/describe"))
        description = ServiceDescription.from_raw(raw)
        if description.contract != CONTRACT_VERSION:
            raise ContractVersionError(
                f"service speaks contract {raw.get('contract')!r}, client speaks {CONTRACT_VERSION}",
                url=self.url("api/describe"),
            )
        with self._lock:
            self._description = description
        return description

    def state(self) -> ServiceState:
        return ServiceState.from_raw(self._request("GET", "api/state"))

    def events(self, since: int = 0) -> EventPage:
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("since must be a nonnegative integer")
        raw = self._request("GET", f"api/events?since={since}")
        if isinstance(raw, list):  # Qumus spec-file style: a bare list
            items, nxt = raw, None
        else:
            items, nxt = raw.get("events", []), raw.get("next")
        events = [Event.from_raw(e) for e in items if isinstance(e, dict)]
        if not isinstance(nxt, int):
            nxt = max([since] + [e.seq for e in events])
        return EventPage(events=events, next=nxt, restarted=nxt < since)

    # ----------------------------------------------------------------- lease
    def heartbeat(self) -> dict[str, Any]:
        """Take the lease, or renew it if this client already holds it. 409 if another holds it."""
        reply = self._request("POST", "api/heartbeat", {})
        return reply.get("lease", reply) if isinstance(reply, dict) else {}

    def renew(self) -> dict[str, Any]:
        """Renew only a lease this client still holds (never re-acquires). 409/404 otherwise."""
        reply = self._request("POST", "api/renew", {})
        return reply.get("lease", reply) if isinstance(reply, dict) else {}

    def release(self) -> dict[str, Any]:
        """Give the lease back. Does not stop running motion."""
        reply = self._request("POST", "api/release", {})
        return reply.get("lease", reply) if isinstance(reply, dict) else {}

    def lease_keeper(self, **kwargs: Any) -> LeaseKeeper:
        return LeaseKeeper(self, **kwargs)

    # -------------------------------------------------------------- commands
    def command_spec(self, action: str) -> CommandSpec:
        description = self.describe()
        spec = description.commands.get(action)
        if spec is None:
            raise BadRequest(
                f"{description.service or self.name} has no action {action!r}; "
                f"offered: {sorted(description.commands)}",
                url=self.url("api/command"),
            )
        return spec

    def command(self, action: str, params: dict[str, Any] | None = None,
                *, command_id: str | None = None) -> CommandReceipt:
        """POST api/command once. Returns when the service accepted it (202), not when done."""
        params = dict(params or {})
        if "action" in params:
            raise ValueError("pass the action name as the first argument, not inside params")
        spec = self.command_spec(action)
        if spec.local_only and not _is_loopback(self.host):
            raise Forbidden(f"{action} is local_only (lab PC debug page / probe scripts only)",
                            url=self.url("api/command"))
        if self.guard is not None:
            self.guard(action, params, spec)
        nested = self.body_style == "nested" or (
            self.body_style == "auto" and self.describe().uses_actions_key)
        if nested:
            body: dict[str, Any] = {"action": action, "params": params}
            if command_id is not None:
                body["command_id"] = command_id
        else:
            # ADR 0001 schemas forbid unknown fields, so command_id is not sent here.
            body = {"action": action, **params}
        sent_at = time.time()
        reply = self._request("POST", "api/command", body,
                              timeout_s=self.command_timeout_s, unknown_outcome=True)
        if not isinstance(reply, dict):
            reply = {}
        # Reactor robot answers 200 with the bare state and no command_id.
        state_raw = reply.get("state") if isinstance(reply.get("state"), dict) else reply
        ident = reply.get("command_id")
        return CommandReceipt(
            action=action,
            command_id=str(ident) if ident is not None else command_id,
            state=ServiceState.from_raw(state_raw),
            sent_at=sent_at,
        )

    def wait_for_command(self, receipt: CommandReceipt, *, timeout_s: float = 60.0,
                         poll_s: float = 0.2, raise_on_failure: bool = True) -> LastCommand:
        """Poll api/state until ``last_command`` shows this command's final result.

        Without a command id (reactor robot), the match is: same action, same client,
        finished no earlier than the send time. Read failures are retried until the
        deadline because reads are safe. On timeout the command is NOT stopped.
        """
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while True:
            try:
                state = self.state()
                last_error = None
            except TransportError as exc:
                state, last_error = None, exc
            if state is not None:
                lc = state.last_command
                if lc is not None and lc.finished and self._matches(receipt, lc, state):
                    if raise_on_failure and not lc.ok:
                        raise CommandFailed(
                            f"{receipt.action} ended {lc.result}: {lc.message}",
                            result=lc.result or "", last_command=lc.raw,
                            url=self.url("api/state"),
                        )
                    return lc
            if time.monotonic() >= deadline:
                detail = f" (last read error: {last_error})" if last_error else ""
                raise CommandTimeout(
                    f"{receipt.action} has no final result after {timeout_s:g} s{detail}",
                    url=self.url("api/state"),
                )
            time.sleep(poll_s)

    def _matches(self, receipt: CommandReceipt, lc: LastCommand, state: ServiceState) -> bool:
        if receipt.command_id is not None and lc.id is not None:
            return lc.id == receipt.command_id
        if lc.action != receipt.action or (lc.client and lc.client != self.client_id):
            return False
        if state.busy:
            return False
        return lc.finished_at is None or lc.finished_at >= receipt.sent_at - 1.0

    def run(self, action: str, params: dict[str, Any] | None = None, *,
            timeout_s: float = 60.0, poll_s: float = 0.2) -> LastCommand:
        """command() then wait_for_command(); raises CommandFailed unless the result is ok."""
        receipt = self.command(action, params)
        return self.wait_for_command(receipt, timeout_s=timeout_s, poll_s=poll_s)

    # ------------------------------------------------------------------ stop
    def stop(self, wait_s: float = 0.0, *, attempts: int = 3) -> StopResult:
        """POST api/stop. Any client, no lease. Retried on transport errors (idempotent)."""
        if not 0 <= wait_s <= 15:
            raise ValueError("wait_s must be between 0 and 15 s")
        last: ContractError | None = None
        for attempt in range(max(1, attempts)):
            try:
                reply = self._request("POST", "api/stop", {"wait_s": wait_s},
                                      timeout_s=wait_s + self.timeout_s)
                break
            except TransportError as exc:
                last = exc
                time.sleep(min(0.2 * (attempt + 1), 1.0))
        else:
            assert last is not None
            raise last
        if not isinstance(reply, dict):
            reply = {}
        word = reply.get("stop")
        state_raw = reply.get("state")
        return StopResult(
            stop=word if isinstance(word, str) else "unconfirmed",
            state=ServiceState.from_raw(state_raw) if isinstance(state_raw, dict) else None,
        )


class LeaseKeeper:
    """Background thread that holds one service's control lease.

    ``start()`` takes the lease (raises ``Conflict`` if someone else holds it).
    The thread renews every ``interval_s`` (default: a third of the lease, at
    most 1 s). It uses ``api/renew`` when describe advertises
    ``renew_only_heartbeat`` (reactor services), else ``api/heartbeat``.
    A renewal that gets no reply or a 5xx is retried while the lease granted at
    the last good renewal can still be alive (a busy service answers late); a
    refusal, or no good renewal for a whole lease, sets ``lost``, stores the
    error, calls ``on_lost(error)`` and exits. It never re-acquires: after a loss the service
    has stopped motion and a caller must look at the state first.
    """

    def __init__(self, client: ContractClient, *, interval_s: float | None = None,
                 on_lost: Callable[[ContractError], None] | None = None,
                 release_on_exit: bool = True) -> None:
        self.client = client
        self.interval_s = interval_s
        self.on_lost = on_lost
        self.release_on_exit = release_on_exit
        self.lost = threading.Event()
        self.error: ContractError | None = None
        self.renewals = 0
        self.missed = 0
        self.lease_s = 3.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self.lost.is_set()

    def start(self) -> LeaseKeeper:
        if self._thread is not None:
            raise RuntimeError("lease keeper already started")
        description = self.client.describe()
        self.lease_s = float(description.lease_seconds)
        if self.interval_s is None:
            self.interval_s = max(0.2, min(1.0, self.lease_s / 3.0))
        self.client.heartbeat()
        self._renew = (self.client.renew if description.renew_only_heartbeat
                       else self.client.heartbeat)
        self._thread = threading.Thread(target=self._loop, name=f"lease:{self.client.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        last_ok = time.monotonic()
        while not self._stop.wait(self.interval_s):
            try:
                self._renew()
                self.renewals += 1
                last_ok = time.monotonic()
            except ContractError as exc:
                # A busy service may answer late (no reply / 5xx). Keep renewing while the
                # lease it granted at the last good renewal can still be alive; a refusal
                # (4xx: someone else holds it, or it expired) is final.
                transient = exc.status is None or exc.status >= 500
                if transient and time.monotonic() - last_ok < self.lease_s - self.interval_s:
                    self.missed += 1
                    _log.warning("lease %s: renewal failed (%s); retrying", self.client.name, exc)
                    continue
                _log.warning("lease %s lost: %s", self.client.name, exc)
                self.error = LeaseLost(f"lease renewal failed: {exc}", status=exc.status,
                                       url=exc.url)
                self.lost.set()
                if self.on_lost is not None:
                    try:
                        self.on_lost(self.error)
                    except Exception:  # noqa: BLE001 - a callback must not kill the report
                        pass
                return

    def check(self) -> None:
        """Raise ``LeaseLost`` if the lease was lost. Call before each command."""
        if self.lost.is_set():
            raise self.error or LeaseLost("lease lost")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=(self.interval_s or 1.0) + self.client.timeout_s)
        if self.release_on_exit and not self.lost.is_set():
            try:
                self.client.release()
            except ContractError:
                pass  # expiry releases it anyway

    def __enter__(self) -> LeaseKeeper:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
