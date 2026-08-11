"""Tests for ChangeSet inspection, verification, and rollback HTTP API."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from incidentlens_control_plane.changes.types import ChangeSetStatus


def test_changes_api_never_returns_backup_plaintext(
    client: TestClient, applied_changeset: str
) -> None:
    response = client.get(f"/api/changes/{applied_changeset}")
    assert response.status_code == 200
    assert "DATABASE_PASSWORD" not in response.text
    assert response.json()["status"] == "applied"
    assert response.json()["files"][0]["local_backup_ref"] == "backup.enc"


def test_changes_api_returns_file_details(
    client: TestClient, applied_changeset: str
) -> None:
    response = client.get(f"/api/changes/{applied_changeset}")
    assert response.status_code == 200
    body = response.json()
    assert body["files"][0]["remote_path"] == "/opt/payments/.env"
    assert body["files"][0]["applied"] is True
    assert body["rollback_plan"] != ""


def test_changes_api_unknown_changeset_returns_404(
    client: TestClient,
) -> None:
    response = client.get("/api/changes/chs-missing")
    assert response.status_code == 404


def test_verify_moves_applied_to_verified(
    client: TestClient, applied_changeset: str
) -> None:
    response = client.post(
        f"/api/changes/{applied_changeset}/verify", json={"result": "ok"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "verified"


def test_verify_failure_moves_applied_to_failed(
    client: TestClient, applied_changeset: str
) -> None:
    response = client.post(
        f"/api/changes/{applied_changeset}/verify", json={"result": "failed"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_verify_requires_applied_status(
    client: TestClient, applied_changeset: str
) -> None:
    # The fixture changeset is applied; verifying again after a terminal
    # transition must be rejected.
    client.post(f"/api/changes/{applied_changeset}/verify", json={"result": "ok"})
    repeated = client.post(
        f"/api/changes/{applied_changeset}/verify", json={"result": "ok"}
    )
    assert repeated.status_code == 409


def test_rollback_interrupting_service_requires_approval(
    client: TestClient, applied_changeset: str, runtime
) -> None:
    missing = client.post(f"/api/changes/{applied_changeset}/rollback", json={})
    assert missing.status_code == 409

    changeset = runtime.change_store.get(applied_changeset)
    assert changeset is not None
    intent = {
        "kind": "rollback",
        "changeset_id": changeset.changeset_id,
        "target_id": changeset.target_id,
        "service": changeset.service_name,
    }
    record = asyncio.run(runtime.approvals.request(intent))
    asyncio.run(runtime.approvals.approve(record.approval_id))

    accepted = client.post(
        f"/api/changes/{applied_changeset}/rollback",
        json={"approval_id": record.approval_id},
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "rolling_back"

    # The target is resolved from the registered project record, so the
    # background rollback consumes the single-use approval and completes the
    # restore before the response returns.
    after = next(
        item for item in runtime.approvals.list() if item.approval_id == record.approval_id
    )
    assert after.status.value == "consumed"
    assert after.consumed_at is not None
    assert runtime.change_store.get(applied_changeset).status is ChangeSetStatus.ROLLED_BACK


def test_rollback_unknown_changeset_returns_404(
    client: TestClient,
) -> None:
    response = client.post("/api/changes/chs-missing/rollback", json={})
    assert response.status_code == 404
