"""Durable runtime-event helpers for the Phase 4 investigation domain.

Every publisher method emits through the shared ``/api/events`` store and
broker (no second stream), so Phase 5 can filter investigation, run and
approval events by the IDs every payload carries.  Payloads contain only
IDs, statuses, counts and bounded redacted summaries: raw logs, raw command
output, credentials, canonical approval intents, backup plaintext and hidden
reasoning never appear in an event payload.  ``emit`` appends to the durable
store synchronously and delivers to live subscribers without blocking, so it
is safe to call from both async and synchronous orchestration paths.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    Investigation,
    RegistryUpdateProposal,
    ToolCall,
)

_JsonValue = str | int | float | bool | None | list[Any] | dict[str, Any]


class InvestigationEventPublisher:
    """Publishes redacted investigation events through the shared stream."""

    def __init__(
        self,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
    ) -> None:
        self._events = events
        self._broker = broker

    def emit(
        self,
        event_type: RuntimeEventType,
        *,
        occurred_at: datetime | None = None,
        **payload: _JsonValue,
    ) -> RuntimeEvent:
        """Append one event durably and deliver it to live subscribers."""
        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=event_type,
            occurred_at=(occurred_at or datetime.now(UTC)).astimezone(UTC),
            payload=dict(payload),
        )
        stored = self._events.append(event)
        self._publish(stored)
        return stored

    def _publish(self, stored: RuntimeEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. a synchronous unit test): publish inline.
            asyncio.run(self._broker.publish(stored))
        else:
            loop.create_task(self._broker.publish(stored))

    # -- investigation --------------------------------------------------------

    def investigation_created(
        self, investigation: Investigation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.INVESTIGATION_CREATED,
            occurred_at=occurred_at,
            investigation_id=investigation.investigation_id,
            incident_id=investigation.incident_id,
            project_id=investigation.project_id,
            target_id=investigation.target_id,
            status=investigation.status.value,
        )

    def investigation_started(
        self,
        investigation: Investigation,
        run: AgentRun,
        *,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.INVESTIGATION_STARTED,
            occurred_at=occurred_at,
            investigation_id=investigation.investigation_id,
            incident_id=investigation.incident_id,
            status=investigation.status.value,
            run_id=run.agent_run_id,
        )

    def investigation_status_changed(
        self,
        investigation: Investigation,
        *,
        previous: str,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.INVESTIGATION_STATUS_CHANGED,
            occurred_at=occurred_at,
            investigation_id=investigation.investigation_id,
            incident_id=investigation.incident_id,
            previous=previous,
            status=investigation.status.value,
        )

    def investigation_completed(
        self, investigation: Investigation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.INVESTIGATION_COMPLETED,
            occurred_at=occurred_at,
            investigation_id=investigation.investigation_id,
            incident_id=investigation.incident_id,
            status=investigation.status.value,
            stop_reason=investigation.stop_reason.value
            if investigation.stop_reason is not None
            else None,
        )

    def investigation_cancelled(
        self, investigation: Investigation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.INVESTIGATION_CANCELLED,
            occurred_at=occurred_at,
            investigation_id=investigation.investigation_id,
            incident_id=investigation.incident_id,
            status=investigation.status.value,
        )

    def investigation_failed(
        self, investigation: Investigation, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.INVESTIGATION_FAILED,
            occurred_at=occurred_at,
            investigation_id=investigation.investigation_id,
            incident_id=investigation.incident_id,
            status=investigation.status.value,
        )

    # -- agent runs -----------------------------------------------------------

    def agent_run_started(
        self, run: AgentRun, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.AGENT_RUN_STARTED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            kind=run.kind.value,
            status=run.status.value,
        )

    def agent_run_status_changed(
        self,
        run: AgentRun,
        *,
        previous: str,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.AGENT_RUN_STATUS_CHANGED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            parent_run_id=run.parent_run_id,
            kind=run.kind.value,
            previous=previous,
            status=run.status.value,
            stop_reason=run.stop_reason.value if run.stop_reason is not None else None,
        )

    def agent_run_completed(
        self, run: AgentRun, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.AGENT_RUN_COMPLETED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            kind=run.kind.value,
            status=run.status.value,
            stop_reason=run.stop_reason.value if run.stop_reason is not None else None,
            rounds=run.usage.rounds,
            tool_calls=run.usage.tool_calls,
            evidence_count=run.usage.evidence_count,
        )

    def agent_run_failed(
        self, run: AgentRun, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.AGENT_RUN_FAILED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            kind=run.kind.value,
            status=run.status.value,
        )

    def agent_run_cancelled(
        self, run: AgentRun, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.AGENT_RUN_CANCELLED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            kind=run.kind.value,
            status=run.status.value,
        )

    # -- tool calls -----------------------------------------------------------

    def tool_call_started(
        self, tool_call: ToolCall, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_CALL_STARTED,
            occurred_at=occurred_at,
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            tool_name=tool_call.tool_name,
            status=tool_call.status.value,
        )

    def tool_call_status_changed(
        self,
        tool_call: ToolCall,
        *,
        previous: str,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_CALL_STATUS_CHANGED,
            occurred_at=occurred_at,
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            tool_name=tool_call.tool_name,
            previous=previous,
            status=tool_call.status.value,
            approval_id=tool_call.approval_id,
            output_bytes=tool_call.output_bytes,
        )

    def tool_call_completed(
        self, tool_call: ToolCall, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_CALL_COMPLETED,
            occurred_at=occurred_at,
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            tool_name=tool_call.tool_name,
            status=tool_call.status.value,
            approval_id=tool_call.approval_id,
            output_bytes=tool_call.output_bytes,
            evidence_count=len(tool_call.evidence_ids),
        )

    # -- child runs -----------------------------------------------------------

    def child_run_started(
        self, run: AgentRun, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.CHILD_RUN_STARTED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            parent_run_id=run.parent_run_id,
            status=run.status.value,
        )

    def child_run_completed(
        self, run: AgentRun, *, occurred_at: datetime | None = None
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.CHILD_RUN_COMPLETED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            parent_run_id=run.parent_run_id,
            status=run.status.value,
            stop_reason=run.stop_reason.value if run.stop_reason is not None else None,
        )

    # -- evidence -------------------------------------------------------------

    def evidence_appended(
        self,
        run: AgentRun,
        *,
        added: int,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.EVIDENCE_APPENDED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            added=added,
            total=len(run.evidence),
        )

    # -- registry proposals ---------------------------------------------------

    def registry_proposal_created(
        self,
        proposal: RegistryUpdateProposal,
        *,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.REGISTRY_PROPOSAL_CREATED,
            occurred_at=occurred_at,
            proposal_id=proposal.proposal_id,
            investigation_id=proposal.investigation_id,
            run_id=proposal.agent_run_id,
            kind=proposal.kind.value,
            status=proposal.status.value,
        )

    def registry_proposal_decided(
        self,
        proposal: RegistryUpdateProposal,
        *,
        decision: str,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.REGISTRY_PROPOSAL_DECIDED,
            occurred_at=occurred_at,
            proposal_id=proposal.proposal_id,
            investigation_id=proposal.investigation_id,
            run_id=proposal.agent_run_id,
            kind=proposal.kind.value,
            status=proposal.status.value,
            decision=decision,
        )

    # -- recovery -------------------------------------------------------------

    def recovery_started(
        self,
        *,
        count: int,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.RECOVERY_STARTED,
            occurred_at=occurred_at,
            count=count,
        )

    def recovery_completed(
        self,
        *,
        count: int,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.RECOVERY_COMPLETED,
            occurred_at=occurred_at,
            count=count,
        )


__all__ = [
    "InvestigationEventPublisher",
]
