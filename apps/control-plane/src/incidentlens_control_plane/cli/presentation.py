"""Pure semantic presentation mapping for runtime events."""

from __future__ import annotations

import os
from dataclasses import dataclass

from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


@dataclass(frozen=True, slots=True)
class EventPresentation:
    symbol: str
    label: str
    color: str
    title: str
    metadata: str
    key: str | None = None


_MODEL = {RuntimeEventType.MODEL_ROUND_STARTED, RuntimeEventType.MODEL_ROUND_COMPLETED}
_OBSERVE = {
    RuntimeEventType.TOOL_PROPOSED,
    RuntimeEventType.TOOL_CALL_STARTED,
    RuntimeEventType.TOOL_CALL_COMPLETED,
    RuntimeEventType.EVIDENCE_APPENDED,
    RuntimeEventType.REMOTE_OPERATION_STARTED,
    RuntimeEventType.REMOTE_OPERATION_COMPLETED,
}


def present_event(event: RuntimeEvent, *, no_color: bool | None = None) -> EventPresentation:
    payload = event.payload
    event_type = event.event_type
    if event_type in _MODEL:
        symbol, label, color = "◆", "MODEL", "#58a6ff"
    elif event_type in _OBSERVE:
        symbol, label, color = "◉", "OBSERVE", "#39c5cf"
    elif event_type is RuntimeEventType.HYPOTHESIS_CHANGED:
        symbol, label, color = "?", "HYPOTHESIS", "#bc8cff"
    elif event_type in {RuntimeEventType.CHILD_RUN_STARTED, RuntimeEventType.CHILD_RUN_COMPLETED}:
        symbol, label, color = "↳", "SUBAGENT", "#bc8cff"
    elif event_type is RuntimeEventType.CONTEXT_COMPACTED:
        symbol, label, color = "⇣", "COMPACT", "#bc8cff"
    elif event_type in {
        RuntimeEventType.APPROVAL_REQUESTED,
        RuntimeEventType.POLICY_DECIDED,
        RuntimeEventType.SAFETY_STATE_CHANGED,
    }:
        symbol, label, color = "⏸", "APPROVAL", "#d29922"
    elif event_type in {
        RuntimeEventType.CHANGESET_CREATED,
        RuntimeEventType.CHANGESET_STATUS_CHANGED,
    }:
        symbol, label, color = "⚙", "APPLY", "#3fb950"
    elif event_type is RuntimeEventType.CHANGESET_ROLLED_BACK:
        symbol, label, color = "↶", "RECOVERY", "#3fb950"
    elif event_type in {
        RuntimeEventType.RECOVERY_STARTED,
        RuntimeEventType.RECOVERY_COMPLETED,
    }:
        symbol, label, color = "↶", "RECOVERY", "#bc8cff"
    elif event_type in {
        RuntimeEventType.INVESTIGATION_COMPLETED,
        RuntimeEventType.AGENT_RUN_COMPLETED,
    }:
        symbol, label, color = "■", "CONCLUSION", "#3fb950"
    else:
        failed = "failed" in event_type.value or payload.get("status") in {
            "failed",
            "rejected",
            "cancelled",
            "uncertain",
        }
        symbol, label, color = ("!", "FAILURE", "#f85149") if failed else ("✓", "VERIFY", "#3fb950")
    title = str(
        payload.get("tool_name")
        or payload.get("status")
        or payload.get("summary_preview")
        or event_type.value
    )[:200]
    ids = [
        f"seq={event.sequence}",
        *(
            f"{name}={payload[name]}"
            for name in ("run_id", "tool_call_id", "approval_id", "changeset_id")
            if payload.get(name)
        ),
    ]
    if payload.get("duration_ms") is not None:
        ids.append(f"duration={payload['duration_ms']}ms")
    if payload.get("evidence_count") is not None:
        ids.append(f"evidence={payload['evidence_count']}")
    if no_color is None:
        no_color = bool(os.environ.get("NO_COLOR"))
    return EventPresentation(
        symbol=symbol,
        label=label,
        color="" if no_color else color,
        title=title,
        metadata=" · ".join(ids),
        key=str(payload.get("tool_call_id")) if payload.get("tool_call_id") else None,
    )


def render_markup(card: EventPresentation) -> str:
    prefix = f"{card.symbol} {card.label}"
    if card.color:
        prefix = f"[bold {card.color}]{prefix}[/]"
    return f"{prefix}  {card.title}  [#8b949e]{card.metadata}[/]"


__all__ = ["EventPresentation", "present_event", "render_markup"]
