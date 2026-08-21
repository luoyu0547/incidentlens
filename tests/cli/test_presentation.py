from datetime import UTC, datetime

from incidentlens_control_plane.cli.presentation import present_event, render_markup
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def event(event_type: RuntimeEventType, **payload) -> RuntimeEvent:
    return RuntimeEvent(
        event_id="evt-1",
        sequence=7,
        event_type=event_type,
        occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
        payload=payload,
    )


def test_semantic_symbols_and_palette() -> None:
    assert present_event(event(RuntimeEventType.MODEL_ROUND_STARTED)).label == "MODEL"
    assert present_event(event(RuntimeEventType.CONTEXT_COMPACTED)).symbol == "⇣"
    assert present_event(event(RuntimeEventType.CHILD_RUN_STARTED)).symbol == "↳"
    assert present_event(event(RuntimeEventType.APPROVAL_REQUESTED)).symbol == "⏸"
    assert present_event(event(RuntimeEventType.CHANGESET_ROLLED_BACK)).symbol == "↶"
    assert present_event(event(RuntimeEventType.INVESTIGATION_COMPLETED)).symbol == "■"


def test_no_color_preserves_symbol_and_text() -> None:
    card = present_event(
        event(RuntimeEventType.TOOL_CALL_STARTED, tool_name="log_query"),
        no_color=True,
    )
    rendered = render_markup(card)
    assert card.color == ""
    assert "OBSERVE" in rendered
    assert card.symbol in rendered
    assert "log_query" in rendered


def test_running_tool_card_is_keyed_by_internal_id() -> None:
    card = present_event(
        event(
            RuntimeEventType.TOOL_CALL_STARTED,
            tool_call_id="tool-run-1-a1",
            provider_tool_call_id="tq1",
            tool_name="container_read",
            duration_ms=12,
        )
    )
    assert card.key == "tool-run-1-a1"
    assert "duration=12ms" in card.metadata
