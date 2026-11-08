"""Instrument service contract v1: client, typed replies and a fake service for tests.

The agent talks to every lab window (Qumus exfoliation and stacking modules,
reactor services) only through this package.
"""

from .client import CONTRACT_VERSION, DEFAULT_CLIENT_HEADER, ContractClient, LeaseKeeper
from .errors import (
    BadRequest,
    CommandFailed,
    CommandTimeout,
    Conflict,
    ContractError,
    ContractVersionError,
    DeviceUnavailable,
    Forbidden,
    LeaseLost,
    NotFound,
    OutcomeUnknown,
    ServerError,
    TransportError,
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

__all__ = [
    "CONTRACT_VERSION", "DEFAULT_CLIENT_HEADER", "BadRequest", "CommandFailed",
    "CommandReceipt", "CommandSpec", "CommandTimeout", "Conflict", "ContractClient",
    "ContractError", "ContractVersionError", "DeviceUnavailable", "Event", "EventPage",
    "Forbidden", "LastCommand", "LeaseKeeper", "LeaseLost", "NotFound", "OutcomeUnknown",
    "ServerError", "ServiceDescription", "ServiceState", "StopResult", "TransportError",
]
