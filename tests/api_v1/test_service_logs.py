from __future__ import annotations

from datetime import UTC

from fastapi.testclient import TestClient
from incidentlens_control_plane.logs.cursors import encode_log_cursor
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
)


def make_record(message: str, *, dedupe_key: str) -> LogRecord:
    from datetime import datetime

    return LogRecord(
        log_id=f"log-{dedupe_key}",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor=f"offset:{dedupe_key}",
        dedupe_key=dedupe_key,
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        event_time=None,
        severity=LogSeverity.INFO,
        message_redacted=message,
        redaction_summary={},
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def _headers(client: TestClient) -> dict[str, str]:
    return dict(client.AUTH_HEADERS)


def _seed(client: TestClient) -> None:
    client.post(
        "/api/projects",
        json={
            "project_id": "payments",
            "display_name": "Payments",
            "targets": [
                {
                    "target_id": "dev-a",
                    "host": "dev-a.example.test",
                    "ssh_user": "deploy",
                    "port": 22,
                }
            ],
            "services": [
                {
                    "compose_service": "payment-api",
                    "container_names": ["payments-api-1"],
                    "allowed_host_paths": ["/var/log/payment"],
                    "protected_remote_paths": [],
                }
            ],
        },
    )
    runtime = client.app.state.runtime
    records = tuple(
        make_record(f"message-{index}", dedupe_key=f"api-{index}").model_copy(
            update={"log_id": f"api-log-{index}", "service_name": "payment-api"}
        )
        for index in range(3)
    )
    runtime.log_store.append_batch(records)


def test_service_log_history_returns_redacted_cursor_page(authenticated_client: TestClient) -> None:
    _seed(authenticated_client)
    response = authenticated_client.get(
        "/api/v1/services/payment-api/logs",
        params={"limit": 2},
        headers=_headers(authenticated_client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["message"] for item in body["items"]] == ["message-0", "message-1"]
    assert body["has_more"] is True
    assert body["next_cursor"].startswith("lc1_")
    assert body["items"][0]["fields"] == {}


def test_service_log_history_rejects_bad_cursor_without_restart(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get(
        "/api/v1/services/payment-api/logs",
        params={"after": "not-a-cursor"},
        headers=_headers(authenticated_client),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cursor_invalid"


def test_service_log_history_rejects_mutual_cursors(authenticated_client: TestClient) -> None:
    cursor = encode_log_cursor(1)
    response = authenticated_client.get(
        "/api/v1/services/payment-api/logs",
        params={"before": cursor, "after": cursor},
        headers=_headers(authenticated_client),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cursor_invalid"
