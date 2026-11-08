from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple

from sqlalchemy import func, select

from database.db_utils import process_session_scope
from database import models

InstrumentStatus = models.InstrumentStatus
InstrumentStatusLog = models.InstrumentStatusLog


class InstrumentStatusRepository:
    """Low-level persistence helpers for instrument status snapshots and logs."""

    def ensure_status(self, name: str, default: dict[str, Any]) -> Tuple[dict[str, Any], bool]:
        """Ensure a status row exists; create with default payload if missing."""
        with process_session_scope() as session:
            status = session.execute(select(InstrumentStatus).where(InstrumentStatus.name == name)).scalar_one_or_none()
            created = False
            if status is None:
                status = InstrumentStatus(name=name, data_json=default)
                session.add(status)
                session.flush()
                created = True
            data = status.data_json or {}
            return dict(data), created

    def read_status(self, name: str) -> dict[str, Any]:
        with process_session_scope() as session:
            status = session.execute(select(InstrumentStatus).where(InstrumentStatus.name == name)).scalar_one_or_none()
            if status is None or status.data_json is None:
                return {}
            return dict(status.data_json)

    def write_status(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        with process_session_scope() as session:
            status = session.execute(select(InstrumentStatus).where(InstrumentStatus.name == name)).scalar_one_or_none()
            if status is None:
                status = InstrumentStatus(name=name, data_json=payload)
                session.add(status)
            else:
                status.data_json = payload
            session.flush()
            data = status.data_json or {}
            return dict(data)

    def append_status_log(
        self,
        event: str,
        category: Optional[str],
        payload: Optional[dict[str, Any]],
    ) -> None:
        with process_session_scope() as session:
            session.add(
                InstrumentStatusLog(
                    event=event,
                    category=category,
                    payload_json=payload or {},
                )
            )

    def append_status_logs(
        self,
        entries: Iterable[tuple[str, Optional[str], dict[str, Any]]],
    ) -> None:
        entries = list(entries)
        if not entries:
            return
        with process_session_scope() as session:
            session.add_all(InstrumentStatusLog(event=event, category=category, payload_json=payload or {}) for event, category, payload in entries)

    def has_any_logs(self) -> bool:
        with process_session_scope() as session:
            count = session.execute(select(func.count()).select_from(InstrumentStatusLog)).scalar_one()
            return count > 0


_default_repository: InstrumentStatusRepository | None = None


def get_status_repository() -> InstrumentStatusRepository:
    global _default_repository
    if _default_repository is None:
        _default_repository = InstrumentStatusRepository()
    return _default_repository


def ensure_status(name: str, default: dict[str, Any], *, repo: InstrumentStatusRepository | None = None) -> Tuple[dict[str, Any], bool]:
    repository = repo or get_status_repository()
    return repository.ensure_status(name, default)


def read_status(name: str, *, repo: InstrumentStatusRepository | None = None) -> dict[str, Any]:
    repository = repo or get_status_repository()
    return repository.read_status(name)


def write_status(name: str, payload: dict[str, Any], *, repo: InstrumentStatusRepository | None = None) -> dict[str, Any]:
    repository = repo or get_status_repository()
    return repository.write_status(name, payload)


def append_status_log(
    event: str,
    category: Optional[str],
    payload: Optional[dict[str, Any]],
    *,
    repo: InstrumentStatusRepository | None = None,
) -> None:
    repository = repo or get_status_repository()
    repository.append_status_log(event, category, payload)


def append_status_logs(
    entries: Iterable[tuple[str, Optional[str], dict[str, Any]]],
    *,
    repo: InstrumentStatusRepository | None = None,
) -> None:
    repository = repo or get_status_repository()
    repository.append_status_logs(entries)


def has_any_logs(*, repo: InstrumentStatusRepository | None = None) -> bool:
    repository = repo or get_status_repository()
    return repository.has_any_logs()
