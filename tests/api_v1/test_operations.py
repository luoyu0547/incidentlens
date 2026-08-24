"""End-to-end tests for the durable operations surface (/api/v1/operations).

These exercise the authenticated v1 surface with three principals:

- ``operator-a``  — unrestricted target access, all scopes;
- ``scoped-b``    — restricted to ``tgt-b``, ``read`` + ``operate`` scopes;
- ``read-only``   — ``read`` scope only.

The tests cover the owner/target authorization rule (404, never leaking
existence), private payload omission, and idempotent, persist-keyed
cancellation with the stable ``operation_not_cancellable`` envelope.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

ROUTE = "/api/v1/operations"

OPERATOR_A_TOKEN = "operator-a-bearer-token"
OPERATOR_A_DIGEST = hashlib.sha256(OPERATOR_A_TOKEN.encode()).hexdigest()
SCOPED_B_TOKEN = "scoped-b-bearer-token"
SCOPED_B_DIGEST = hashlib.sha256(SCOPED_B_TOKEN.encode()).hexdigest()
READ_ONLY_TOKEN = "read-only-bearer-token"
READ_ONLY_DIGEST = hashlib.sha256(READ_ONLY_TOKEN.encode()).hexdigest()

_PROFILES = [
    {
        "principal_id": "operator-a",
        "display_name": "Operator A",
        "scopes": ["read", "operate", "approve", "admin"],
        "token_digest": OPERATOR_A_DIGEST,
    },
    {
        "principal_id": "scoped-b",
        "display_name": "Scoped B",
        "scopes": ["read", "operate"],
        "allowed_target_ids": ["tgt-b"],
        "token_digest": SCOPED_B_DIGEST,
    },
    {
        "principal_id": "read-only",
        "display_name": "Read Only",
        "scopes": ["read"],
        "token_digest": READ_ONLY_DIGEST,
    },
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A client over an app with the three test principals configured."""
    settings = RuntimeSettings(
        data_dir=tmp_path / "data",
        auth_profiles_json=json.dumps(_PROFILES),
        secure_cookies=False,
    )
    app = create_app(settings, transport_factory=FakeTransportFactory())
    with TestClient(app) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem(token: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _seed(
    client: TestClient,
    *,
    target_id: str = "tgt-a",
    created_by: str = "operator-a",
    status: OperationStatus = OperationStatus.QUEUED,
    session_id: str | None = None,
    investigation_id: str | None = None,
    progress_summary: str | None = "checking",
    request_payload: str | None = None,
) -> object:
    runtime = client.app.state.runtime
    service = runtime.operation_service
    op = service.create_operation(
        kind=OperationKind.TARGET_TEST,
        target_id=target_id,
        created_by=created_by,
        session_id=session_id,
        investigation_id=investigation_id,
        progress_summary=progress_summary,
        request_payload=request_payload,
        now=datetime.now(UTC),
    )
    if status == OperationStatus.RUNNING:
        service.claim(op.operation_id, worker="worker-1", now=datetime.now(UTC))
    elif status in (
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.UNCERTAIN,
    ):
        service.claim(op.operation_id, worker="worker-1", now=datetime.now(UTC))
        service.transition(op.operation_id, status, now=datetime.now(UTC))
    elif status == OperationStatus.CANCELLED:
        service.cancel(op.operation_id, now=datetime.now(UTC))
    return service.get_operation(op.operation_id)


# -- read surface --------------------------------------------------------------


def test_get_returns_view_and_omits_request_payload(client: TestClient) -> None:
    op = _seed(client, request_payload='{"password":"hunter2","token":"abc123"}')
    response = client.get(f"{ROUTE}/{op.operation_id}", headers=_auth(OPERATOR_A_TOKEN))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operation_id"] == op.operation_id
    assert body["kind"] == "target_test"
    assert body["status"] == "queued"
    assert body["target_id"] == "tgt-a"
    assert "request_payload" not in body
    assert "claim_token" not in body
    assert "created_by" not in body
    assert "hunter2" not in response.text
    assert "abc123" not in response.text


def test_get_returns_session_and_investigation_ids(client: TestClient) -> None:
    op = _seed(client, session_id="sess-1", investigation_id="inv-1")
    body = client.get(
        f"{ROUTE}/{op.operation_id}", headers=_auth(OPERATOR_A_TOKEN)
    ).json()
    assert body["session_id"] == "sess-1"
    assert body["investigation_id"] == "inv-1"


def test_get_unknown_operation_is_404(client: TestClient) -> None:
    response = client.get(f"{ROUTE}/op-missing", headers=_auth(OPERATOR_A_TOKEN))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_get_unauthenticated_is_401(client: TestClient) -> None:
    response = client.get(f"{ROUTE}/op-missing")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_get_unauthorized_target_is_404_not_403(client: TestClient) -> None:
    op = _seed(client, target_id="tgt-other")
    response = client.get(f"{ROUTE}/{op.operation_id}", headers=_auth(SCOPED_B_TOKEN))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_get_authorized_target_succeeds(client: TestClient) -> None:
    op = _seed(client, target_id="tgt-b")
    response = client.get(f"{ROUTE}/{op.operation_id}", headers=_auth(SCOPED_B_TOKEN))
    assert response.status_code == 200
    assert response.json()["target_id"] == "tgt-b"


def test_get_allowed_by_creator_rule(client: TestClient) -> None:
    op = _seed(client, target_id="tgt-any", created_by="scoped-b")
    response = client.get(f"{ROUTE}/{op.operation_id}", headers=_auth(SCOPED_B_TOKEN))
    assert response.status_code == 200
    assert response.json()["operation_id"] == op.operation_id


# -- cancellation surface ------------------------------------------------------


def test_cancel_queued_is_idempotent(client: TestClient) -> None:
    op = _seed(client)
    headers = _idem(OPERATOR_A_TOKEN, "cancel-queued-1")
    first = client.post(f"{ROUTE}/{op.operation_id}/cancel", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "cancelled"
    assert first.json()["finished_at"] is not None

    second = client.post(f"{ROUTE}/{op.operation_id}/cancel", headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"
    assert second.headers.get("Idempotency-Replayed") == "true"

    fresh = client.post(
        f"{ROUTE}/{op.operation_id}/cancel",
        headers=_idem(OPERATOR_A_TOKEN, "cancel-queued-2"),
    )
    assert fresh.status_code == 200
    assert fresh.json()["status"] == "cancelled"


def test_cancel_running_requests_cancellation(client: TestClient) -> None:
    op = _seed(client, status=OperationStatus.RUNNING)
    response = client.post(
        f"{ROUTE}/{op.operation_id}/cancel",
        headers=_idem(OPERATOR_A_TOKEN, "cancel-running-1"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancel_requested"
    assert response.json()["finished_at"] is None


def test_cancel_terminal_statuses_are_operation_not_cancellable(
    client: TestClient,
) -> None:
    for status in (OperationStatus.SUCCEEDED, OperationStatus.FAILED):
        op = _seed(client, status=status)
        response = client.post(
            f"{ROUTE}/{op.operation_id}/cancel",
            headers=_idem(OPERATOR_A_TOKEN, f"cancel-{status.value}-1"),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "operation_not_cancellable"


def test_cancel_missing_idempotency_key_is_422(client: TestClient) -> None:
    op = _seed(client)
    response = client.post(
        f"{ROUTE}/{op.operation_id}/cancel", headers=_auth(OPERATOR_A_TOKEN)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_cancel_read_only_principal_is_403(client: TestClient) -> None:
    op = _seed(client)
    response = client.post(
        f"{ROUTE}/{op.operation_id}/cancel",
        headers=_idem(READ_ONLY_TOKEN, "cancel-ro-1"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cancel_unauthorized_target_is_404(client: TestClient) -> None:
    op = _seed(client, target_id="tgt-other")
    response = client.post(
        f"{ROUTE}/{op.operation_id}/cancel",
        headers=_idem(SCOPED_B_TOKEN, "cancel-scoped-1"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_cancel_unknown_operation_is_404(client: TestClient) -> None:
    response = client.post(
        f"{ROUTE}/op-missing/cancel", headers=_idem(OPERATOR_A_TOKEN, "cancel-missing")
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
