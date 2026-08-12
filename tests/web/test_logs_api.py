"""Tests for the log query and search HTTP APIs."""

from __future__ import annotations

from datetime import UTC, datetime

from incidentlens_control_plane.remote_ops.transport import (
    RemoteConnectionError,
    RemoteTimeoutError,
)

from web.conftest import make_web_log_record


def test_logs_query_rejects_unexpected_connection_fields(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
            "host": "attacker.example.test",
            "ssh_user": "root",
        },
    )

    assert response.status_code == 422


def test_logs_query_rejects_unregistered_container(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "not-registered",
            "tail_lines": 50,
            "persist": False,
            "create_evidence": False,
        },
    )

    assert response.status_code == 409


def test_logs_search_returns_persisted_redacted_records(
    client, runtime, registered_project
) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    runtime.log_store.append_batch((make_web_log_record("ERROR token=[REDACTED_TOKEN]", now=now),))

    response = client.get(
        "/api/logs/search",
        params={"project_id": "payments", "text": "ERROR", "limit": 10},
    )

    assert response.status_code == 200
    assert response.json()[0]["message_redacted"] == "ERROR token=[REDACTED_TOKEN]"
    assert "abc123" not in response.text


def test_logs_query_rejects_unknown_target(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "not-a-target",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Target not found"


def test_logs_query_rejects_unknown_service(client, registered_project) -> None:
    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "not-a-service",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Service not found"


def test_logs_query_maps_remote_timeout_to_504(
    client, runtime, registered_project, monkeypatch
) -> None:
    async def _timeout(query_request, *, now):
        raise RemoteTimeoutError("timed out")

    monkeypatch.setattr(runtime.logs, "query", _timeout)

    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
        },
    )

    assert response.status_code == 504
    assert response.json()["detail"] == "Log source timed out"


def test_logs_query_maps_remote_connection_error_to_502(
    client, runtime, registered_project, monkeypatch
) -> None:
    async def _connection_error(query_request, *, now):
        raise RemoteConnectionError("connection failed")

    monkeypatch.setattr(runtime.logs, "query", _connection_error)

    response = client.post(
        "/api/logs/query",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service_name": "payment-api",
            "source_kind": "docker",
            "scope": "container",
            "source_ref": "payments-api-1",
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Log source unavailable"


def test_logs_search_filters_by_time_window(client, runtime, registered_project) -> None:
    runtime.log_store.append_batch(
        (
            make_web_log_record(
                "ERROR early",
                now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
                log_id="log-web-early",
            ),
            make_web_log_record(
                "ERROR late",
                now=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
                log_id="log-web-late",
            ),
        )
    )

    response = client.get(
        "/api/logs/search",
        params={
            "project_id": "payments",
            "start_time": "2026-08-12T11:00:00Z",
            "end_time": "2026-08-12T13:00:00Z",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["message_redacted"] == "ERROR late"
