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
    Conclusion,
    Hypothesis,
    Investigation,
    RegistryUpdateProposal,
    SessionMemory,
    TodoItem,
    ToolCall,
)
from incidentlens_control_plane.logs.redaction import redact_message

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
        self,
        run: AgentRun,
        *,
        reason: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.AGENT_RUN_FAILED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            kind=run.kind.value,
            status=run.status.value,
            reason_preview=self._preview(reason) if reason else None,
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

    # -- semantic agent lifecycle ----------------------------------------------

    def model_round_started(
        self, run: AgentRun, *, round_number: int, occurred_at: datetime
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.MODEL_ROUND_STARTED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            round_number=round_number,
            status=run.status.value,
        )

    def model_round_completed(
        self,
        run: AgentRun,
        *,
        round_number: int,
        input_tokens: int,
        output_tokens: int,
        output_bytes: int,
        duration_ms: int,
        stop_reason: str | None,
        occurred_at: datetime,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.MODEL_ROUND_COMPLETED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            round_number=round_number,
            status=run.status.value,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_bytes=output_bytes,
            duration_ms=max(0, duration_ms),
            stop_reason=stop_reason,
        )

    def tool_proposed(
        self,
        run: AgentRun,
        *,
        tool_call_id: str,
        provider_tool_call_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        occurred_at: datetime,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_PROPOSED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            tool_call_id=tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            tool_name=tool_name,
            summary=self._tool_display_summary(tool_name, arguments),
            arguments_preview=self._preview(arguments),
            status="proposed",
        )

    @staticmethod
    def _tool_display_summary(tool_name: str, arguments: dict[str, Any]) -> str:
        """Return a bounded product-facing description, never raw argument JSON."""
        if tool_name == "registry_info":
            return "确认已注册的调查范围"
        if tool_name == "todo_write":
            todos = arguments.get("todos")
            count = len(todos) if isinstance(todos, list) else 0
            return f"更新调查计划（{count} 项）"
        if tool_name == "host_metrics":
            sections = arguments.get("sections")
            labels = {"load": "负载", "memory": "内存", "disk": "磁盘"}
            selected = sections if isinstance(sections, list) else ["load", "memory", "disk"]
            visible = "、".join(labels.get(str(item), str(item)) for item in selected)
            return f"检查主机{visible}"
        if tool_name in {"host_read", "container_read"}:
            path = arguments.get("path")
            return f"读取文件 {path}" if isinstance(path, str) else "读取文件"
        if tool_name in {"host_list", "container_list"}:
            path = arguments.get("path")
            return f"列出目录 {path}" if isinstance(path, str) else "列出目录"
        if tool_name in {"host_search", "container_search"}:
            path = arguments.get("path")
            query = arguments.get("query")
            if isinstance(path, str) and isinstance(query, str):
                return f"在 {path} 中搜索 {query}"
            return "搜索文件"
        if tool_name == "shell_exec":
            command = arguments.get("command")
            return f"执行受控命令 {command}" if isinstance(command, str) else "执行受控命令"
        if tool_name.startswith("log_"):
            return "查询已脱敏日志"
        return "执行受控调查步骤"

    def policy_decided(
        self,
        run: AgentRun,
        *,
        tool_call_id: str,
        tool_name: str,
        decision: str,
        requires_approval: bool,
        occurred_at: datetime,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.POLICY_DECIDED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            decision=decision,
            requires_approval=requires_approval,
        )

    def todo_changed(
        self, run: AgentRun, items: tuple[TodoItem, ...], *, occurred_at: datetime
    ) -> RuntimeEvent:
        counts = {
            status: sum(item.status.value == status for item in items)
            for status in ("pending", "in_progress", "completed")
        }
        return self.emit(
            RuntimeEventType.TODO_CHANGED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            total=len(items),
            items=[
                {
                    "todo_id": item.todo_id,
                    "content": item.content,
                    "status": item.status.value,
                }
                for item in items
            ],
            **counts,
        )

    def hypothesis_changed(
        self, hypothesis: Hypothesis, *, occurred_at: datetime
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.HYPOTHESIS_CHANGED,
            occurred_at=occurred_at,
            run_id=hypothesis.agent_run_id,
            hypothesis_id=hypothesis.hypothesis_id,
            status=hypothesis.status.value,
            summary_preview=self._preview(hypothesis.summary),
            evidence_count=len(hypothesis.evidence_ids),
        )

    def context_compacted(
        self,
        run: AgentRun,
        memory: SessionMemory,
        *,
        mode: str,
        occurred_at: datetime,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.CONTEXT_COMPACTED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            mode=mode,
            memory_revision=memory.revision,
            through_sequence=memory.through_transcript_sequence,
            recipe_count=len(memory.reacquisition_recipes),
            immutable_count=len(memory.immutable_observations),
            pending_count=len(memory.pending_actions),
            safety_count=len(memory.safety_state),
        )

    def safety_state_changed(
        self,
        run: AgentRun,
        *,
        status: str,
        reason: str,
        occurred_at: datetime,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.SAFETY_STATE_CHANGED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            status=status,
            reason_preview=self._preview(reason),
        )

    @staticmethod
    def _preview(value: object, width: int = 600) -> str:
        return redact_message(str(value), max_length=width).message_redacted

    # -- product session projection --------------------------------------------

    def agent_text_delta(
        self,
        *,
        session_id: str,
        message_id: str,
        run_id: str,
        text: str,
        occurred_at: datetime,
    ) -> RuntimeEvent:
        """Emit one redacted assistant text projection delta."""
        return self.emit(
            RuntimeEventType.AGENT_TEXT_DELTA,
            occurred_at=occurred_at,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            text=self._preview(text, width=20_000),
        )

    def agent_message_completed(
        self,
        *,
        session_id: str,
        message_id: str,
        run_id: str,
        transcript_sequence: int,
        occurred_at: datetime,
    ) -> RuntimeEvent:
        """Emit completion after the projected message is durable."""
        return self.emit(
            RuntimeEventType.AGENT_MESSAGE_COMPLETED,
            occurred_at=occurred_at,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            transcript_sequence=transcript_sequence,
        )

    def tool_call_started(
        self,
        tool_call: ToolCall,
        *,
        investigation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_CALL_STARTED,
            occurred_at=occurred_at,
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            investigation_id=investigation_id,
            tool_name=tool_call.tool_name,
            status=tool_call.status.value,
        )

    def tool_call_status_changed(
        self,
        tool_call: ToolCall,
        *,
        previous: str,
        investigation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_CALL_STATUS_CHANGED,
            occurred_at=occurred_at,
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            investigation_id=investigation_id,
            tool_name=tool_call.tool_name,
            previous=previous,
            status=tool_call.status.value,
            approval_id=tool_call.approval_id,
            output_bytes=tool_call.output_bytes,
        )

    def tool_call_completed(
        self,
        tool_call: ToolCall,
        *,
        investigation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        return self.emit(
            RuntimeEventType.TOOL_CALL_COMPLETED,
            occurred_at=occurred_at,
            tool_call_id=tool_call.tool_call_id,
            run_id=tool_call.agent_run_id,
            investigation_id=investigation_id,
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

    def conclusion_created(
        self,
        run: AgentRun,
        conclusion: Conclusion,
        *,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        """Publish one grounded conclusion without raw tool or log content."""
        return self.emit(
            RuntimeEventType.CONCLUSION_CREATED,
            occurred_at=occurred_at,
            run_id=run.agent_run_id,
            investigation_id=run.investigation_id,
            evidence_ids=list(conclusion.evidence_ids),
            conclusion=conclusion.model_dump(mode="json"),
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
