"""One link per laboratory module: contract 2 (lab-contract's labclient) or contract v1.

The job service (stations/jobs.py) talks to a module only through a link:

    commands()            the module's command list, for the job catalogue
    lease()               hold the module's control lease (renewed in the background)
    command(a, params)    send one command and wait for its end; returns its result data
    stop(wait_s)          stop the module (any client, no lease)
    release()             give the lease back

`open_link` picks the path per module when the brain connects:

* contract 2 when the station answers `GET api/station` with `contract: 2` and lists a service
  named like the module, and labclient can be loaded. The entry is the module's URL unless
  stations.yaml gives `entry:`; the service name is the module's unless it gives `service:`.
* contract v1 otherwise, or when stations.yaml says `contract: 1` for the module.
  `contract: 2` makes contract 2 required (connecting fails without it).

The record feed, pictures, put_records and the [Q-sim] page endpoints stay on the v1 routes for
now (stations/mirror.py, simview.py): contract 2's records profile with brain-issued sample ids
comes in a later step (lab-contract spec section 11, decision 0008).

labclient lives in the private papaya2003/lab-contract repository and is not a package
dependency. It is found as an installed package, in the folder `QUMUS_LAB_CONTRACT` names, or in a
`lab-contract` checkout next to this repository.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from qumus2.contract import (
    BadRequest,
    CommandFailed,
    Conflict,
    ContractClient,
    ContractError,
    ContractVersionError,
    Forbidden,
    LeaseKeeper,
    LeaseLost,
    NotFound,
    OutcomeUnknown,
    ServerError,
    TransportError,
)
from qumus2.contract.models import CommandSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)
CONTRACT_CHOICES = ("auto", "1", "2")


class StationLink(Protocol):
    module: str
    contract: int

    def commands(self) -> dict[str, CommandSpec]: ...
    def lease(self) -> None: ...
    def command(self, action: str, params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]: ...
    def stop(self, wait_s: float = 1.0) -> None: ...
    def release(self) -> None: ...


# --------------------------------------------------------------------------- contract v1
class V1Link:
    """The v1 path: ContractClient + LeaseKeeper (unchanged behaviour)."""

    contract = 1

    def __init__(self, module: str, client: ContractClient) -> None:
        self.module, self.client = module, client
        self._keeper: LeaseKeeper | None = None

    def commands(self) -> dict[str, CommandSpec]:
        return self.client.describe(refresh=True).commands

    def lease(self) -> None:
        if self._keeper is not None and self._keeper.active:
            return
        try:
            self.client.heartbeat()
        except Conflict as e:
            raise ValueError(f"the {self.module} module is controlled by someone else: {e}") from e
        self._keeper = self.client.lease_keeper().start()

    def command(self, action: str, params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        client = self.client
        receipt = client.command(action, params)
        lc = (receipt.state.raw or {}).get("last_command") or {}
        if receipt.command_id is not None and lc.get("id") == receipt.command_id \
                and lc.get("result") not in (None, "running"):
            if lc.get("result") != "ok":
                raise ValueError(f"{action} on {self.module}: {lc.get('result')}: {lc.get('message')}")
            return lc.get("data") or {}
        # not in the reply: find this command's end in the state
        done = client.wait_for_command(receipt, timeout_s=timeout_s)
        return dict(done.raw.get("data") or {})

    def stop(self, wait_s: float = 1.0) -> None:
        self.client.stop(wait_s=wait_s)

    def release(self) -> None:
        if self._keeper is not None:
            self._keeper.stop()
            self._keeper = None
        try:
            self.client.release()
        except ContractError:
            pass


# --------------------------------------------------------------------------- contract 2
def load_labclient() -> Any | None:
    """lab-contract's labclient, or None when it cannot be found."""
    try:
        import labclient  # noqa: PLC0415

        return labclient
    except ImportError:
        pass
    for folder in (os.environ.get("QUMUS_LAB_CONTRACT"), REPO_ROOT.parent / "lab-contract"):
        if folder and (Path(folder) / "labclient" / "__init__.py").is_file():
            path = str(Path(folder).resolve())
            if path not in sys.path:
                sys.path.append(path)
            try:
                import labclient  # noqa: PLC0415

                return labclient
            except ImportError:
                return None
    return None


def offers_contract2(entry: str, timeout_s: float = 3.0) -> dict[str, Any] | None:
    """The station entry body when `entry` answers contract 2, else None (any error)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = entry.rstrip("/") + "/api/station"
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                         timeout=timeout_s) as reply:
            body = json.loads(reply.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) and body.get("contract") == 2 else None


_V2_CODES: dict[str, type[ContractError]] = {
    "bad_request": BadRequest, "unknown_action": BadRequest, "bad_params": BadRequest,
    "bad_content_type": BadRequest, "local_only": Forbidden, "forbidden": Forbidden,
    "not_found": NotFound, "busy": Conflict, "locked": Conflict, "maintenance": Conflict,
    "lease_held": Conflict, "lease_required": Conflict, "id_reused": Conflict,
    "sample_mismatch": Conflict, "lease_lost": LeaseLost, "device_offline": ServerError,
    "internal": ServerError,
}


class V2Link:
    """The contract 2 path: one labclient Service. Errors become qumus2.contract errors, so the
    job service handles both paths alike."""

    contract = 2

    def __init__(self, module: str, service: Any, labclient: Any) -> None:
        self.module, self.service, self.lc = module, service, labclient
        self.url = service.http.base
        self._stack: contextlib.ExitStack | None = None
        self._keeper: Any = None

    @contextlib.contextmanager
    def _errors(self) -> Iterator[None]:
        lc = self.lc
        try:
            yield
        except lc.Refused as e:
            cls = _V2_CODES.get(e.code, ContractError)
            raise cls(f"{e.code}: {e.message}", status=e.status, url=self.url) from None
        except lc.CommandFailed as e:
            rec = e.record
            raise CommandFailed(f"{rec['action']} ended {rec['result']}: {rec['message']}",
                                result=rec["result"], last_command=rec, url=self.url) from None
        except lc.LostAnswer as e:
            raise OutcomeUnknown(str(e), url=self.url) from None
        except lc.LeaseLost as e:
            raise LeaseLost(str(e), url=self.url) from None
        except lc.ContractMismatch as e:
            raise ContractVersionError(str(e), url=self.url) from None
        except TimeoutError as e:
            raise TransportError(str(e), url=self.url) from None
        except OSError as e:
            raise TransportError(f"no reply: {e}", url=self.url) from None

    def commands(self) -> dict[str, CommandSpec]:
        with self._errors():
            self.service.reload()
        out = {}
        for name, spec in self.service.describe["commands"].items():
            out[name] = CommandSpec(name, spec["params"], any_client=not spec["lease"],
                                    motion=spec["motion"], local_only=spec["local_only"], raw=spec)
        return out

    def lease(self) -> None:
        if self._keeper is not None and not self._keeper.lost:
            return
        self.release()
        stack = contextlib.ExitStack()
        try:
            with self._errors():
                self._keeper = stack.enter_context(self.service.lease())
        except Conflict as e:
            stack.close()
            raise ValueError(f"the {self.module} module is controlled by someone else: {e}") from e
        self._stack = stack

    def command(self, action: str, params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        try:
            with self._errors():
                record = self.service.run(action, params, timeout_s=timeout_s)
        except CommandFailed as e:
            raise ValueError(f"{action} on {self.module}: {e.message}") from e
        return dict(record.get("data") or {})

    def stop(self, wait_s: float = 1.0, *, attempts: int = 3) -> None:
        """Stop is idempotent: a transport error is retried (as ContractClient.stop does)."""
        for attempt in range(max(1, attempts)):
            try:
                with self._errors():
                    self.service.stop(wait_s)
                return
            except TransportError:
                if attempt == attempts - 1:
                    raise
                time.sleep(min(0.2 * (attempt + 1), 1.0))

    def release(self) -> None:
        stack, self._stack, self._keeper = self._stack, None, None
        if stack is not None:
            try:
                stack.close()  # stops the renewals and gives the lease back
            except Exception:  # noqa: BLE001 - expiry releases it anyway
                pass


# --------------------------------------------------------------------------- choosing
def open_link(module: str, client: ContractClient, options: dict[str, Any] | None = None) -> StationLink:
    """The link for one module (see the module docstring for how the path is chosen)."""
    options = options or {}
    choice = str(options.get("contract", "auto"))
    if choice not in CONTRACT_CHOICES:
        raise ValueError(f"stations.yaml: {module}.contract must be auto, 1 or 2, got {choice!r}")
    if choice == "1":
        return V1Link(module, client)
    entry = str(options.get("entry") or client.base_url)
    name = str(options.get("service") or module)
    body = offers_contract2(entry, timeout_s=client.timeout_s)
    lc = load_labclient() if body is not None else None
    usable = lc is not None and any(s.get("name") == name for s in body.get("services", []))
    if not usable:
        if choice == "2":
            why = ("no contract 2 station there" if body is None else
                   "labclient not found (lab-contract checkout)" if lc is None else
                   f"the station has no service {name!r}")
            raise ContractVersionError(f"{module}: contract 2 required but {why}", url=entry)
        if body is not None:
            log.warning("%s offers contract 2 but %s; using contract v1", module,
                        "labclient was not found" if lc is None else f"has no service {name!r}")
        return V1Link(module, client)
    try:
        station = lc.connect(entry, client=client.client_id, timeout_s=client.timeout_s)
        service = station.service(name)
    except (lc.ContractMismatch, lc.Refused, OSError) as e:
        if choice == "2":
            raise ContractVersionError(f"{module}: {e}", url=entry) from None
        log.warning("%s: contract 2 refused (%s); using contract v1", module, e)
        return V1Link(module, client)
    log.info("%s: contract 2 via %s (service %s)", module, entry, name)
    return V2Link(module, service, lc)
