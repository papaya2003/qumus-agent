"""Errors raised by the contract v1 client.

Every error keeps the HTTP status (or None for transport failures), the service
URL and the service's own message. The client never retries a command; the
caller decides what to do after reading the state.
"""

from __future__ import annotations


class ContractError(Exception):
    """Base class: a contract call did not succeed."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        self.message = str(message)
        self.status = status
        self.url = url
        prefix = f"{status} " if status is not None else ""
        super().__init__(f"{prefix}{self.message}" + (f" ({url})" if url else ""))


class ContractVersionError(ContractError):
    """describe reported a contract other than 1; the client refuses to talk to it."""


class BadRequest(ContractError):
    """400, 413, 415 or 422: the request itself was invalid (unknown action, bad params)."""


class Forbidden(ContractError):
    """403: refused by policy, e.g. a local_only action sent from another host."""


class NotFound(ContractError):
    """404: the route does not exist on this service (e.g. api/renew on an older service)."""


class Conflict(ContractError):
    """409: busy, locked, lease missing or held by another client."""


class DeviceUnavailable(ContractError):
    """503: the service lost its device link."""


class ServerError(ContractError):
    """Any other 5xx answer."""


class TransportError(ContractError):
    """No HTTP answer: connection refused, reset, DNS failure or timeout.

    For a read this only means the service is unreachable. For a command it
    means the outcome is unknown; see OutcomeUnknown.
    """


class OutcomeUnknown(TransportError):
    """A command or heartbeat was sent but no reply arrived.

    The command may or may not have started. Contract v1 says the service locks
    itself in that case and a person must check the device. Never resend a motion
    command after this error; read the state first.
    """


class CommandFailed(ContractError):
    """A command finished with result failed, rejected or stopped."""

    def __init__(self, message: str, *, result: str, last_command: dict | None = None,
                 url: str = "") -> None:
        self.result = result
        self.last_command = last_command or {}
        super().__init__(message, url=url)


class CommandTimeout(ContractError):
    """wait_for_command gave up before the service reported a result.

    The command may still be running. The client does not stop it automatically;
    call stop() if that is what the caller wants.
    """


class LeaseLost(ContractError):
    """The lease keeper could not renew the lease. Motion on that service has stopped."""


_STATUS_MAP: dict[int, type[ContractError]] = {
    400: BadRequest,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    413: BadRequest,
    415: BadRequest,
    422: BadRequest,
    503: DeviceUnavailable,
}


def error_for_status(status: int, message: str, url: str = "") -> ContractError:
    """Map an HTTP error status to the matching exception class."""
    cls = _STATUS_MAP.get(status)
    if cls is None:
        cls = ServerError if status >= 500 else ContractError
    return cls(message, status=status, url=url)
