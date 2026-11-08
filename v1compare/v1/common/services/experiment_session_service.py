from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from database import models
from database.db_utils import experiment_session_scope


@dataclass(frozen=True)
class ExperimentSessionRecord:
    id: int
    session_key: Optional[str]
    user_id: Optional[str]
    title: Optional[str]
    user_goal: Optional[str]
    workflow_summary: Optional[str]
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ExperimentMessageRecord:
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime
    attachments: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ExperimentWorkflowRecord:
    id: int
    session_id: int
    workflow_type: str
    description: Optional[str]
    definition: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class ExperimentRunRecord:
    id: int
    session_id: int
    workflow_id: Optional[int]
    run_type: str
    status: str
    parameters: dict[str, Any] | None
    result_summary: Optional[str]
    result: dict[str, Any] | None
    user_feedback: Optional[str]
    process_run_id: Optional[int]
    started_at: datetime
    completed_at: Optional[datetime]


class ExperimentSessionService:
    """High-level manager for experiment sessions shared by design/physics agents."""

    def create_session(
        self,
        *,
        session_key: Optional[str] = None,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
        user_goal: Optional[str] = None,
        workflow_summary: Optional[str] = None,
        status: str = "draft",
        metadata: Optional[dict[str, Any]] = None,
    ) -> ExperimentSessionRecord:
        with experiment_session_scope() as session:
            row = models.ExperimentSession(
                session_key=session_key,
                user_id=user_id,
                title=title,
                user_goal=user_goal,
                workflow_summary=workflow_summary,
                status=status,
                metadata_json=metadata or {},
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return self._to_session_record(row)

    def get_or_create_session(self, *, session_key: str, **kwargs: Any) -> ExperimentSessionRecord:
        with experiment_session_scope() as session:
            row = (
                session.query(models.ExperimentSession)
                .filter(models.ExperimentSession.session_key == session_key)
                .one_or_none()
            )
            if row is None:
                row = models.ExperimentSession(session_key=session_key, **kwargs)
                session.add(row)
                session.flush()
                session.refresh(row)
            return self._to_session_record(row)

    def get_session(self, session_id: int) -> Optional[ExperimentSessionRecord]:
        with experiment_session_scope() as session:
            row = session.get(models.ExperimentSession, session_id)
            if row is None:
                return None
            session.expunge(row)
            return self._to_session_record(row)

    def append_message(
        self,
        session_id: int,
        role: str,
        content: str,
        *,
        attachments: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ExperimentMessageRecord:
        with experiment_session_scope() as session:
            row = models.ExperimentMessage(
                session_id=session_id,
                role=role,
                content=content,
                attachments_json=attachments or [],
                metadata_json=metadata or {},
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return self._to_message_record(row)

    def attach_workflow(
        self,
        session_id: int,
        workflow_type: str,
        *,
        description: Optional[str],
        definition: dict[str, Any],
    ) -> ExperimentWorkflowRecord:
        with experiment_session_scope() as session:
            row = models.ExperimentWorkflow(
                session_id=session_id,
                workflow_type=workflow_type,
                description=description,
                definition_json=definition,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return self._to_workflow_record(row)

    def record_run(
        self,
        session_id: int,
        *,
        workflow_id: Optional[int],
        run_type: str,
        status: str,
        parameters: Optional[dict[str, Any]] = None,
        result_summary: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        user_feedback: Optional[str] = None,
        process_run_id: Optional[int] = None,
    ) -> ExperimentRunRecord:
        with experiment_session_scope() as session:
            row = models.ExperimentRun(
                session_id=session_id,
                workflow_id=workflow_id,
                run_type=run_type,
                status=status,
                parameters_json=parameters,
                result_summary=result_summary,
                result_json=result,
                user_feedback=user_feedback,
                process_run_id=process_run_id,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return self._to_run_record(row)

    def link_chat_history(self, chat_history_id: int, session_id: int) -> None:
        with experiment_session_scope() as session:
            entry = session.get(models.ChatHistory, chat_history_id)
            if entry is None:
                raise ValueError(f"ChatHistory {chat_history_id} not found")
            entry.experiment_session_id = session_id

    def get_session_overview(self, session_id: int) -> dict[str, Any]:
        with experiment_session_scope() as session:
            session_row = session.get(models.ExperimentSession, session_id)
            if session_row is None:
                raise ValueError(f"ExperimentSession {session_id} not found")
            overview = self._to_session_record(session_row)
            messages = [
                self._to_message_record(row)
                for row in session.query(models.ExperimentMessage)
                .filter(models.ExperimentMessage.session_id == session_id)
                .order_by(models.ExperimentMessage.created_at.asc())
                .all()
            ]
            workflows = [
                self._to_workflow_record(row)
                for row in session.query(models.ExperimentWorkflow)
                .filter(models.ExperimentWorkflow.session_id == session_id)
                .order_by(models.ExperimentWorkflow.created_at.asc())
                .all()
            ]
            runs = [
                self._to_run_record(row)
                for row in session.query(models.ExperimentRun)
                .filter(models.ExperimentRun.session_id == session_id)
                .order_by(models.ExperimentRun.started_at.asc())
                .all()
            ]
            return {
                "session": overview,
                "messages": messages,
                "workflows": workflows,
                "runs": runs,
            }

    def _to_session_record(self, row: models.ExperimentSession) -> ExperimentSessionRecord:
        return ExperimentSessionRecord(
            id=row.id,
            session_key=row.session_key,
            user_id=row.user_id,
            title=row.title,
            user_goal=row.user_goal,
            workflow_summary=row.workflow_summary,
            status=row.status,
            metadata=row.metadata_json or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _to_message_record(self, row: models.ExperimentMessage) -> ExperimentMessageRecord:
        return ExperimentMessageRecord(
            id=row.id,
            session_id=row.session_id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
            attachments=list(row.attachments_json or []),
            metadata=row.metadata_json or {},
        )

    def _to_workflow_record(self, row: models.ExperimentWorkflow) -> ExperimentWorkflowRecord:
        return ExperimentWorkflowRecord(
            id=row.id,
            session_id=row.session_id,
            workflow_type=row.workflow_type,
            description=row.description,
            definition=row.definition_json or {},
            created_at=row.created_at,
        )

    def _to_run_record(self, row: models.ExperimentRun) -> ExperimentRunRecord:
        return ExperimentRunRecord(
            id=row.id,
            session_id=row.session_id,
            workflow_id=row.workflow_id,
            run_type=row.run_type,
            status=row.status,
            parameters=row.parameters_json,
            result_summary=row.result_summary,
            result=row.result_json,
            user_feedback=row.user_feedback,
            process_run_id=row.process_run_id,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
