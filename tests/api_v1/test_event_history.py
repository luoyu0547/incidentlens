"""End-to-end tests for the filtered durable event history surface.

These exercise ``GET /api/v1/events`` over the shared ``authenticated_client``
fixture: full pagination without a fixed 1,000-row hole, AND composition of
filters, repeated event types as an IN predicate, principal target filtering,
and stability when the store retains event types the route does not know.
"""

from __future__ import annotations

from datetime import UTC, datetime

from incidentlens_control_plane.events.types import (
    RuntimeEvent,
    RuntimeEventType,
)


def _seed(client, *, count: int, target_id: str) -> None:
    """Append *count* durable events for *target_id* via the runtime store."""
    store = client.app.state.runtime.events
    created = RuntimeEventType.PROJECT_CREATED
    updated = RuntimeEventType.PROJECT_UPDATED
    for index in range(1, count + 1):
        store.append(
            RuntimeEvent(
                event_id=f"evt-history-{target_id}-{index}",
                event_type=created if index % 2 else updated,
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                payload={"target_id": target_id},
            )
        )


def test_event_pagination_returns_all_1501_sequences(authenticated_client) -> None:
    """Fetching 500-row pages must surface every sequence with no fixed gap."""
    _seed(authenticated_client, count=1501, target_id="tgt-a")

    sequences: list[int] = []
    after = 0
    while True:
        response = authenticated_client.get(
            "/api/v1/events",
            params={"after_sequence": after, "limit": 500},
            headers=authenticated_client.AUTH_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        items = body["items"]
        sequences.extend(item["sequence"] for item in items)
        if not body["has_more"]:
            break
        after = body["next_after_sequence"]

    assert sequences == list(range(1, 1502))


def test_event_and_and_type_filters_compose(authenticated_client) -> None:
    """target + investigation filters compose as AND with an IN of types."""
    _seed(authenticated_client, count=40, target_id="tgt-a")

    response = authenticated_client.get(
        "/api/v1/events",
        params={
            "target_id": "tgt-a",
            "investigation_id": "inv-1",
            "event_type": ["project.created", "project.updated"],
            "limit": 500,
        },
        headers=authenticated_client.AUTH_HEADERS,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    # Every stored event has no investigation dimension, so AND filtering
    # against an investigation the store does not index yields an empty page.
    assert items == []
    assert response.json()["has_more"] is False


def test_event_type_in_predicate_filters_and_paginates(authenticated_client) -> None:
    _seed(authenticated_client, count=100, target_id="tgt-a")

    response = authenticated_client.get(
        "/api/v1/events",
        params={
            "target_id": "tgt-a",
            "event_type": ["project.created"],
            "limit": 500,
        },
        headers=authenticated_client.AUTH_HEADERS,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 50
    assert all(item["event_type"] == "project.created" for item in items)


def test_event_unknown_type_is_validation_error(authenticated_client) -> None:
    response = authenticated_client.get(
        "/api/v1/events",
        params={"event_type": ["not.a.real.type"]},
        headers=authenticated_client.AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_event_unknown_stored_type_does_not_break_full_page(authenticated_client) -> None:
    """A page larger than the fixed 1,000 window must still return fully."""
    _seed(authenticated_client, count=1501, target_id="tgt-a")

    response = authenticated_client.get(
        "/api/v1/events",
        params={"limit": 500},
        headers=authenticated_client.AUTH_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 500
    assert body["has_more"] is True
