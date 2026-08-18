"""Fixed, non-authorizing lifecycle hooks for the agent runtime."""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.events.types import JsonValue, RuntimeEventType
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher


class HookEventType(StrEnum):
    """The fixed lifecycle events exposed to internal runtime callbacks."""

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    TOOL_ERROR = "ToolError"
    SUBAGENT_START = "SubAgentStart"
    SUBAGENT_STOP = "SubAgentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"


class HookEvent(BaseModel):
    """Immutable, bounded hook payload; callbacks cannot alter execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: HookEventType
    agent_run_id: str
    action_name: str
    occurred_at: datetime
    status: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


HookCallback = Callable[[HookEvent], Any]


class HookRunner:
    """Run registered callbacks in registration order and isolate failures."""

    def __init__(self) -> None:
        self._callbacks: dict[HookEventType, list[HookCallback]] = defaultdict(list)

    def register(self, event_type: HookEventType, callback: HookCallback) -> None:
        self._callbacks[event_type].append(callback)

    async def emit(self, event: HookEvent) -> tuple[str, ...]:
        failures: list[str] = []
        for callback in self._callbacks[event.event_type]:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # hooks never affect enforcement
                kind = type(exc).__qualname__
                failures.append(kind)
        return tuple(failures)


class RuntimeHookRecorder:
    """Persist hook observations in the shared runtime event stream."""

    def __init__(self, publisher: InvestigationEventPublisher) -> None:
        self._publisher = publisher

    def __call__(self, event: HookEvent) -> None:
        self._publisher.emit(
            RuntimeEventType.AGENT_HOOK,
            occurred_at=event.occurred_at,
            hook_type=event.event_type,
            agent_run_id=event.agent_run_id,
            action_name=event.action_name,
            status=event.status,
            metadata=event.metadata,
        )


__all__ = ["HookEvent", "HookEventType", "HookRunner", "RuntimeHookRecorder"]
