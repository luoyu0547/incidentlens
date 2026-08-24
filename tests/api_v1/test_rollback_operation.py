"""End-to-end tests for ``POST /api/v1/changesets/{id}/rollback``.

The route validates the changeset and approval gate, then enqueues a durable
``ROLLBACK`` operation and answers ``202 OperationAccepted``.  The dispatcher
(running in the app lifespan) executes the restore through the registered
rollback handler, consuming the approval and moving the changeset to
``rolled_back``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest
from auth.helpers import OPERATOR_A_TOKEN
from fastapi.testclient import TestClient
from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.operations.types import OperationKind, OperationStatus
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

ROUTE = "/api/v1/changesets"

PROJECT_PAYLOAD = {
    "project_id": "payments",
    "display_name": "Payments",
    "local_source_paths": ["/srv/payments"],
    "targets": [
        {
            "target_id": "dev-a",
            "host": "dev-a.example.test",
            "ssh_user": "deploy",
        }
    ],
    "services": [
        {
            "compose_service": "payment-api",
            "container_names": ["payments-api-1"],
            "local_source_path": "/srv/payments",
            "allowed_host_paths": ["/opt/payments"],
            "protected_remote_paths": ["/opt/payments/.env"],
        }
    ],
}


RESTRICTED_B_TOKEN = "restricted-b-bearer-token"
RESTRICTED_B_DIGEST = hashlib.sha256(RESTRICTED_B_TOKEN.encode()).hexdigest()
OPERATOR_A_DIGEST = hashlib.sha256(OPERATOR_A_TOKEN.encode()).hexdigest()

_PROFILES = [
    {
        "principal_id": "operator-a",
        "display_name": "Operator A",
        "scopes": ["read", "operate", "approve", "admin"],
        "token_digest": OPERATOR_A_DIGEST,
    },
    {
        "principal_id": "restricted-b",
        "display_name": "Restricted B",
        "scopes": ["read", "operate"],
        "allowed_target_ids": ["tgt-other"],
        "token_digest": RESTRICTED_B_DIGEST,
    },
]


@pytest.fixture
def restricted_client(tmp_path: Path) -> TestClient:
    """A client whose principal can only address ``tgt-other`` targets."""
    app = create_app(
        RuntimeSettings(
            data_dir=tmp_path / "data",
            auth_profiles_json=json.dumps(_PROFILES),
            secure_cookies=False,
        ),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as client:
        yield client


def _headers(client: TestClient, key: str) -> dict[str, str]:
    """Bearer-authenticated headers carrying an ``Idempotency-Key``."""
    return {"Idempotency-Key": key, **client.AUTH_HEADERS}


def _seed_project(client: TestClient) -> None:
    response = client.post("/api/projects", json=PROJECT_PAYLOAD)
    assert response.status_code == 201, response.text


def _create_applied_changeset(
    runtime, *, changeset_id: str = "chs-v1-1", remote_path: str = "/opt/payments/.env"
) -> str:
    store = runtime.change_store
    store.create_changeset(
        changeset_id=changeset_id,
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        files=(
            FileChange(
                file_change_id="op-1",
                scope="host",
                remote_path=remote_path,
                expected_sha256=None,
                replacement_sha256="b" * 64,
                diff_text="",
                original_metadata={},
                local_backup_ref="backup.enc",
                remote_backup_path="/opt/payments/.env.incidentlens-backup.20260810T120000.000000Z",
                temp_path="/opt/payments/.env.incidentlens-tmp-chs-v1-1",
                applied=True,
                validation_result="validated",
                rollback_result=None,
            ),
        ),
        verification_plan="run syntax checks and compare service behavior",
        rollback_plan="restore the verified timestamped backup",
    )
    for status in (
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
    ):
        store.transition(changeset_id, status)
    return changeset_id


def _approved_approval(runtime, changeset_id: str) -> str:
    changeset = runtime.change_store.get(changeset_id)
    intent = {
        "kind": "rollback",
        "changeset_id": changeset.changeset_id,
        "target_id": changeset.target_id,
        "service": changeset.service_name,
    }
    record = asyncio.run(runtime.approvals.request(intent))
    asyncio.run(runtime.approvals.approve(record.approval_id))
    return record.approval_id


def _wait_rolled_back(runtime, changeset_id: str, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = runtime.change_store.get(changeset_id)
        if current is not None and current.status is ChangeSetStatus.ROLLED_BACK:
            return
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {changeset_id} to roll back")


def _wait_terminal(
    operation_store, operation_id: str, *, timeout: float = 5.0
) -> str:
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


def test_rollback_interrupting_changeset_requires_approval(
    authenticated_client: TestClient,
) -> None:
    _seed_project(authenticated_client)
    runtime = authenticated_client.app.state.runtime
    changeset_id = _create_applied_changeset(runtime)

    missing = authenticated_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={},
        headers=_headers(authenticated_client, "rollback-missing-approval"),
    )
    assert missing.status_code == 409
    assert "approval is required" in missing.json()["error"]["message"]


def test_rollback_enqueues_operation_and_completes(
    authenticated_client: TestClient,
) -> None:
    _seed_project(authenticated_client)
    runtime = authenticated_client.app.state.runtime
    changeset_id = _create_applied_changeset(runtime)
    approval_id = _approved_approval(runtime, changeset_id)

    response = authenticated_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={"approval_id": approval_id},
        headers=_headers(authenticated_client, "rollback-1"),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] is True
    operation_id = body["operation_id"]

    operation = runtime.operation_service.get_operation(operation_id)
    assert operation.kind == OperationKind.ROLLBACK
    assert operation.target_id == "dev-a"

    _wait_rolled_back(runtime, changeset_id)
    status = _wait_terminal(runtime.operation_store, operation_id)
    # The durable operation completed via the dispatcher, and the single-use
    # approval was consumed exactly once.
    assert status in (OperationStatus.SUCCEEDED, OperationStatus.FAILED)
    consumed = [
        item
        for item in runtime.approvals.list()
        if item.approval_id == approval_id
    ]
    assert len(consumed) == 1
    assert consumed[0].status.value == "consumed"


def test_rollback_replays_idempotently(authenticated_client: TestClient) -> None:
    _seed_project(authenticated_client)
    runtime = authenticated_client.app.state.runtime
    changeset_id = _create_applied_changeset(runtime)
    approval_id = _approved_approval(runtime, changeset_id)

    headers = _headers(authenticated_client, "rollback-replay")
    first = authenticated_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={"approval_id": approval_id},
        headers=headers,
    )
    assert first.status_code == 202, first.text
    second = authenticated_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={"approval_id": approval_id},
        headers=headers,
    )
    assert second.status_code == 202, second.text
    assert second.json()["operation_id"] == first.json()["operation_id"]
    assert second.headers.get("Idempotency-Replayed") == "true"


def test_rollback_unknown_changeset_is_404(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(
        f"{ROUTE}/chs-missing/rollback",
        json={},
        headers=_headers(authenticated_client, "rollback-missing"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_rollback_non_rollbackable_status_is_409(
    authenticated_client: TestClient,
) -> None:
    _seed_project(authenticated_client)
    runtime = authenticated_client.app.state.runtime
    changeset_id = _create_applied_changeset(runtime)
    # APPLIED -> VALIDATED is still rollback-able; VALIDATED -> VERIFIED locks it.
    runtime.change_store.transition(changeset_id, ChangeSetStatus.VALIDATED)
    runtime.change_store.transition(changeset_id, ChangeSetStatus.VERIFIED)

    response = authenticated_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={},
        headers=_headers(authenticated_client, "rollback-verified"),
    )
    assert response.status_code == 409
    assert "cannot roll back" in response.json()["error"]["message"]


def test_rollback_missing_idempotency_key_is_422(
    authenticated_client: TestClient,
) -> None:
    _seed_project(authenticated_client)
    runtime = authenticated_client.app.state.runtime
    changeset_id = _create_applied_changeset(runtime)
    approval_id = _approved_approval(runtime, changeset_id)

    response = authenticated_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={"approval_id": approval_id},
        headers=authenticated_client.AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_rollback_unauthorized_target_is_404_no_leak(
    restricted_client: TestClient,
) -> None:
    """A target-restricted principal gets 404 (no existence leak), not 403."""
    _seed_project(restricted_client)
    runtime = restricted_client.app.state.runtime
    changeset_id = _create_applied_changeset(runtime)  # targets "dev-a"

    response = restricted_client.post(
        f"{ROUTE}/{changeset_id}/rollback",
        json={},
        headers={
            "Authorization": f"Bearer {RESTRICTED_B_TOKEN}",
            "Idempotency-Key": "rollback-restricted-1",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    # The unauthorized principal could not enqueue anything: no durable operation,
    # queued or otherwise, exists for the changeset's target.
    assert runtime.operation_store.list_queued() == ()
