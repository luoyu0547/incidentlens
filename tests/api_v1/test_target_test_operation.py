"""End-to-end tests for ``POST /api/v1/targets/{id}/test``.

The route enqueues a durable ``TARGET_TEST`` operation and answers
``202 OperationAccepted``; the dispatcher (running in the app lifespan) executes
the reachability probe through the registered handler.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus

ROUTE = "/api/v1/targets"

CREATE_PAYLOAD = {
    "name": "Payments",
    "host": "payments.example.test",
    "ssh_user": "deploy",
    "ssh_port": 2222,
    "authentication_ref": "ssh-agent:deploy@payments.example.test",
}


def _headers(client: TestClient, key: str) -> dict[str, str]:
    """Bearer-authenticated headers carrying an ``Idempotency-Key``."""
    return {"Idempotency-Key": key, **client.AUTH_HEADERS}


def _create_target(client: TestClient, key: str) -> str:
    response = client.post(ROUTE, json=CREATE_PAYLOAD, headers=_headers(client, key))
    assert response.status_code == 201, response.text
    return response.json()["target_id"]


def _wait_terminal(operation_store, operation_id, *, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = operation_store.get(operation_id).status
        if status in (
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.UNCERTAIN,
            OperationStatus.CANCELLED,
        ):
            return status
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for terminal status of {operation_id}")


def test_target_test_enqueues_operation_and_completes(
    authenticated_client: TestClient,
) -> None:
    runtime = authenticated_client.app.state.runtime
    target_id = _create_target(authenticated_client, "create-tt-1")

    response = authenticated_client.post(
        f"{ROUTE}/{target_id}/test",
        headers=_headers(authenticated_client, "target-test-1"),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] is True
    operation_id = body["operation_id"]

    operation = runtime.operation_service.get_operation(operation_id)
    assert operation.kind == OperationKind.TARGET_TEST
    assert operation.target_id == target_id

    status = _wait_terminal(runtime.operation_store, operation_id)
    assert status == OperationStatus.SUCCEEDED
    assert "reachable=True" in (runtime.operation_store.get(operation_id).progress_summary or "")


def test_target_test_replays_idempotently(authenticated_client: TestClient) -> None:
    runtime = authenticated_client.app.state.runtime
    target_id = _create_target(authenticated_client, "create-tt-2")

    headers = _headers(authenticated_client, "target-test-replay")
    first = authenticated_client.post(f"{ROUTE}/{target_id}/test", headers=headers)
    assert first.status_code == 202, first.text
    second = authenticated_client.post(f"{ROUTE}/{target_id}/test", headers=headers)
    assert second.status_code == 202, second.text
    assert second.json()["operation_id"] == first.json()["operation_id"]
    assert second.headers.get("Idempotency-Replayed") == "true"
    # Exactly one durable operation exists for the replayed pair.
    operation = runtime.operation_service.get_operation(first.json()["operation_id"])
    assert operation.kind == OperationKind.TARGET_TEST


def test_target_test_unknown_target_is_404(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(
        f"{ROUTE}/tgt-missing/test",
        headers=_headers(authenticated_client, "target-test-missing"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_target_test_missing_idempotency_key_is_422(
    authenticated_client: TestClient,
) -> None:
    target_id = _create_target(authenticated_client, "create-tt-3")
    response = authenticated_client.post(
        f"{ROUTE}/{target_id}/test", headers=authenticated_client.AUTH_HEADERS
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_target_test_unauthenticated_is_401(client: TestClient) -> None:
    response = client.post(f"{ROUTE}/tgt-x/test", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
