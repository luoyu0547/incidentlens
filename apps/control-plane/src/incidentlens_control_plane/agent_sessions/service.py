"""Application service for the durable agent-session facade."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from incidentlens_control_plane.agent_sessions.store import AgentSessionStore
from incidentlens_control_plane.agent_sessions.types import (
    AgentMessage,
    AgentMessageAccepted,
    AgentMessageRole,
    AgentMessageView,
    AgentSession,
    AgentSessionPatch,
    AgentSessionStatus,
    AgentSessionView,
)
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    MessageRole,
    TextBlock,
)
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.operations.service import OperationService
from incidentlens_control_plane.operations.types import OperationKind

if TYPE_CHECKING:
    from incidentlens_control_plane.investigation.service import InvestigationService


class AgentSessionForbidden(Exception):
    """The principal does not own the requested session."""


class AgentSessionService:
    """Coordinate session persistence, safe transcript projection and work."""

    def __init__(
        self,
        *,
        sessions: AgentSessionStore,
        operations: OperationService,
        investigations: InvestigationService | None = None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._investigations = investigations
        self._events_pub = (
            InvestigationEventPublisher(events, broker)
            if events is not None and broker is not None
            else None
        )

    def create_session(
        self,
        *,
        principal_id: str,
        target_id: str,
        title: str | None = None,
        service_id: str | None = None,
        now: datetime | None = None,
    ) -> AgentSession:
        timestamp = _utc(now)
        session = AgentSession(
            session_id=f"ses_{uuid.uuid4().hex[:24]}",
            target_id=target_id,
            service_id=service_id,
            title=title,
            owner=principal_id,
            investigation_id=None,
            status=AgentSessionStatus.IDLE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return self._sessions.create_session(session)

    def get_owned(self, session_id: str, principal_id: str) -> AgentSession:
        session = self._sessions.get_session(session_id)
        if session.owner != principal_id:
            raise AgentSessionForbidden("agent session is not accessible")
        return session

    def list_owned(self, principal_id: str) -> tuple[AgentSession, ...]:
        return self._sessions.list_sessions(owner=principal_id)

    def patch_session(
        self,
        session_id: str,
        principal_id: str,
        patch: AgentSessionPatch,
        *,
        now: datetime | None = None,
    ) -> AgentSession:
        current = self.get_owned(session_id, principal_id)
        return self._sessions.update_session(
            current.model_copy(update={"title": patch.title, "updated_at": _utc(now)})
        )

    def accept_message(
        self,
        *,
        session_id: str,
        principal_id: str,
        content: str,
        now: datetime | None = None,
    ) -> AgentMessageAccepted:
        """Persist a redacted user message and enqueue work without executing it."""
        session = self.get_owned(session_id, principal_id)
        timestamp = _utc(now)
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        redacted = redact_message(content, max_length=20_000).message_redacted
        self._sessions.append_message(
            AgentMessage(
                message_id=message_id,
                session_id=session.session_id,
                investigation_id=session.investigation_id,
                agent_run_id=None,
                role=AgentMessageRole.USER,
                content_redacted=redacted,
                transcript_sequence=None,
                created_at=timestamp,
            )
        )
        operation = self._operations.enqueue(
            kind=OperationKind.AGENT_MESSAGE,
            target_id=session.target_id,
            created_by=principal_id,
            session_id=session.session_id,
            investigation_id=session.investigation_id,
            # The worker reads the already-redacted message row.  Raw prompt text
            # is deliberately never copied into an operation payload.
            request_payload=json.dumps(
                {"action": "message", "message_id": message_id},
                sort_keys=True,
                separators=(",", ":"),
            ),
            now=timestamp,
        )
        self._sessions.update_session(
            session.model_copy(
                update={"status": AgentSessionStatus.ACTIVE, "updated_at": timestamp}
            )
        )
        return AgentMessageAccepted(message_id=message_id, operation_id=operation.operation_id)

    def list_messages(
        self,
        session_id: str,
        principal_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 100,
    ) -> tuple[AgentMessageView, ...]:
        self.get_owned(session_id, principal_id)
        return tuple(
            AgentMessageView(
                message_id=message.message_id,
                session_id=message.session_id,
                role=message.role,
                content=message.content_redacted,
                created_at=message.created_at,
            )
            for message in self._sessions.list_messages(
                session_id, after_message_id=after_message_id, limit=limit
            )
        )

    def bind_investigation(
        self,
        session_id: str,
        investigation_id: str,
        *,
        status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
        now: datetime | None = None,
    ) -> AgentSession:
        return self._sessions.bind_investigation(
            session_id, investigation_id, now=_utc(now), status=status
        )

    def sync_investigation(
        self,
        session_id: str,
        investigation_id: str,
        *,
        now: datetime | None = None,
    ) -> AgentSession:
        """Map authoritative Investigation status into the product facade."""
        if self._investigations is None:
            return self._sessions.get_session(session_id)
        investigation = self._investigations.get_investigation(investigation_id)
        status = _session_status(investigation.status)
        session = self._sessions.get_session(session_id)
        return self._sessions.update_session(
            session.model_copy(
                update={
                    "investigation_id": investigation_id,
                    "status": status,
                    "updated_at": _utc(now),
                }
            )
        )

    async def cancel(
        self, session_id: str, principal_id: str, *, now: datetime | None = None
    ) -> AgentSession:
        session = self.get_owned(session_id, principal_id)
        if self._investigations is None or session.investigation_id is None:
            return self._sessions.update_session(
                session.model_copy(
                    update={"status": AgentSessionStatus.CANCELLED, "updated_at": _utc(now)}
                )
            )
        await self._investigations.cancel(session.investigation_id)
        return self.sync_investigation(session_id, session.investigation_id, now=now)

    def resume(
        self, session_id: str, principal_id: str, *, now: datetime | None = None
    ) -> AgentMessageAccepted:
        """Queue a resume action; the dispatcher performs model work asynchronously."""
        session = self.get_owned(session_id, principal_id)
        message_id = (
            self._latest_user_message_id(session_id)
            or f"resume_{uuid.uuid4().hex[:24]}"
        )
        operation = self._operations.enqueue(
            kind=OperationKind.AGENT_MESSAGE,
            target_id=session.target_id,
            created_by=principal_id,
            session_id=session_id,
            investigation_id=session.investigation_id,
            request_payload=json.dumps(
                {"action": "resume", "message_id": message_id},
                sort_keys=True,
                separators=(",", ":"),
            ),
            now=_utc(now),
        )
        self._sessions.update_session(
            session.model_copy(
                update={"status": AgentSessionStatus.ACTIVE, "updated_at": _utc(now)}
            )
        )
        return AgentMessageAccepted(message_id=message_id, operation_id=operation.operation_id)

    def project_run(
        self,
        session_id: str,
        run: AgentRun,
        *,
        user_message_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Project only redacted text blocks from a durable run transcript."""
        if self._investigations is None:
            return 0
        count = 0
        transcript = self._investigations.list_transcript_messages(run.agent_run_id)
        for transcript_message in transcript:
            if transcript_message.role is MessageRole.USER:
                if user_message_id and transcript_message.sequence == 1:
                    self._sessions.bind_message(
                        user_message_id,
                        investigation_id=run.investigation_id,
                        agent_run_id=run.agent_run_id,
                        transcript_sequence=transcript_message.sequence,
                    )
                continue
            text = "\n".join(
                block.text for block in transcript_message.blocks if isinstance(block, TextBlock)
            )
            text = _product_agent_text(text)
            if not text:
                continue
            message_id = _assistant_message_id(run.agent_run_id, transcript_message.sequence)
            redacted = redact_message(text, max_length=20_000).message_redacted
            projected = self._sessions.append_message(
                AgentMessage(
                    message_id=message_id,
                    session_id=session_id,
                    investigation_id=run.investigation_id,
                    agent_run_id=run.agent_run_id,
                    role=AgentMessageRole.ASSISTANT,
                    content_redacted=redacted,
                    transcript_sequence=transcript_message.sequence,
                    created_at=transcript_message.created_at,
                )
            )
            count += 1
            if self._events_pub is not None:
                for offset in range(0, len(projected.content_redacted), 160):
                    self._events_pub.agent_text_delta(
                        session_id=session_id,
                        message_id=projected.message_id,
                        run_id=run.agent_run_id,
                        text=projected.content_redacted[offset : offset + 160],
                        occurred_at=_utc(now),
                    )
                self._events_pub.agent_message_completed(
                    session_id=session_id,
                    message_id=projected.message_id,
                    run_id=run.agent_run_id,
                    transcript_sequence=transcript_message.sequence,
                    occurred_at=_utc(now),
                )
        return count

    def _latest_user_message_id(self, session_id: str) -> str | None:
        messages = self._sessions.list_messages(session_id, limit=500)
        for message in reversed(messages):
            if message.role is AgentMessageRole.USER:
                return message.message_id
        return None

    @staticmethod
    def to_view(session: AgentSession) -> AgentSessionView:
        return AgentSessionView.model_validate(session.model_dump())


def _utc(value: datetime | None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _assistant_message_id(run_id: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{sequence}".encode()).hexdigest()[:24]
    return f"msg_{digest}"


def _product_agent_text(content: str) -> str:
    """Convert structured provider JSON into user-facing narrative text."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content.strip()
    if not isinstance(payload, dict):
        return content.strip()
    summaries: list[str] = []
    for key in ("conclusions", "hypotheses"):
        items = payload.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("summary"), str):
                    summaries.append(item["summary"].strip())
    stop = payload.get("stop") or payload.get("stop_signal")
    if isinstance(stop, dict) and isinstance(stop.get("summary"), str):
        summaries.append(stop["summary"].strip())
    return "\n".join(summary for summary in summaries if summary)


def _session_status(status: InvestigationStatus) -> AgentSessionStatus:
    if status in {InvestigationStatus.CREATED, InvestigationStatus.RUNNING}:
        return AgentSessionStatus.ACTIVE
    if status is InvestigationStatus.WAITING_APPROVAL:
        return AgentSessionStatus.WAITING_APPROVAL
    if status in {
        InvestigationStatus.WAITING_REGISTRY_UPDATE,
        InvestigationStatus.PAUSED_BUDGET,
        InvestigationStatus.PAUSED_MISSING_EVIDENCE,
        InvestigationStatus.PAUSED_UNCERTAIN_STATE,
    }:
        return AgentSessionStatus.PAUSED
    if status is InvestigationStatus.CANCEL_REQUESTED:
        return AgentSessionStatus.CANCEL_REQUESTED
    if status is InvestigationStatus.CANCELLED:
        return AgentSessionStatus.CANCELLED
    if status is InvestigationStatus.FAILED:
        return AgentSessionStatus.FAILED
    if status is InvestigationStatus.COMPLETED:
        return AgentSessionStatus.COMPLETED
    return AgentSessionStatus.ACTIVE


__all__ = ["AgentSessionForbidden", "AgentSessionService"]
