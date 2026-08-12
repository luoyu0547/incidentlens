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


def test_logs_search_filters_by_correlation_key_and_normal_signal(
    client, runtime, registered_project
) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    runtime.log_store.append_batch(
        (
            make_web_log_record("ERROR trace hit", now=now, log_id="log-c1").model_copy(
                update={"correlation_key": "trace:aaa"}
            ),
            make_web_log_record("WARN other trace", now=now, log_id="log-c2").model_copy(
                update={"correlation_key": "trace:bbb"}
            ),
            make_web_log_record("INFO healthcheck", now=now, log_id="log-c3").model_copy(
                update={"normal_signal": "healthcheck_ok"}
            ),
        )
    )

    by_key = client.get(
        "/api/logs/search",
        params={"project_id": "payments", "correlation_key": "trace:aaa"},
    )
    assert by_key.status_code == 200
    assert [row["message_redacted"] for row in by_key.json()] == ["ERROR trace hit"]

    by_signal = client.get(
        "/api/logs/search",
        params={"project_id": "payments", "normal_signal": "healthcheck_ok"},
    )
    assert by_signal.status_code == 200
    assert [row["message_redacted"] for row in by_signal.json()] == [
        "INFO healthcheck"
    ]


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


def test_logs_search_normalizes_non_utc_start_time(
    client, runtime, registered_project
) -> None:
    """A -05:00 start_time must be compared as UTC, not as an offset string."""
    runtime.log_store.append_batch(
        (
            make_web_log_record(
                "ERROR before",
                now=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
                log_id="log-web-before",
            ),
            make_web_log_record(
                "ERROR after",
                now=datetime(2026, 8, 12, 16, 30, tzinfo=UTC),
                log_id="log-web-after",
            ),
        )
    )

    # 11:00 -05:00 == 16:00 UTC, so the 15:00 UTC record is excluded.
    response = client.get(
        "/api/logs/search",
        params={
            "project_id": "payments",
            "start_time": "2026-08-12T11:00:00-05:00",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [record["message_redacted"] for record in body] == ["ERROR after"]
