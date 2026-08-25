from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.changes.types import FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

ROUTE = "/api/v1/approvals"

OPERATOR_A_TOKEN = "operator-a-bearer-token"
SCOPED_B_TOKEN = "scoped-b-bearer-token"
READ_ONLY_TOKEN = "read-only-bearer-token"

_PROFILES = [
    {
        "principal_id": "operator-a",
        "display_name": "Operator A",
        "scopes": ["read", "operate", "approve", "admin"],
        "token_digest": hashlib.sha256(OPERATOR_A_TOKEN.encode()).hexdigest(),
    },
    {
        "principal_id": "scoped-b",
        "display_name": "Scoped B",
        "scopes": ["read", "approve"],
        "allowed_target_ids": ["tgt-b"],
        "token_digest": hashlib.sha256(SCOPED_B_TOKEN.encode()).hexdigest(),
    },
    {
        "principal_id": "read-only",
        "display_name": "Read Only",
        "scopes": ["read"],
        "token_digest": hashlib.sha256(READ_ONLY_TOKEN.encode()).hexdigest(),
    },
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(
        RuntimeSettings(
            data_dir=tmp_path / "data",
            auth_profiles_json=json.dumps(_PROFILES),
            secure_cookies=False,
        ),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem(token: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _seed_changeset(runtime, *, changeset_id: str, target_id: str, service: str) -> None:
    runtime.change_store.create_changeset(
        changeset_id=changeset_id,
        incident_id="inc-1",
        project_id="proj-1",
        target_id=target_id,
        service_name=service,
        files=(
            FileChange(
                file_change_id=f"fc-{changeset_id}",
                scope="host",
                remote_path="/opt/app/.env",
                expected_sha256="a" * 64,
                replacement_sha256="b" * 64,
                diff_text="-PASSWORD=secret\n+PASSWORD=[REDACTED]\n",
                original_metadata={},
                local_backup_ref="vault://backup",
                remote_backup_path="/opt/app/.env.backup",
            ),
        ),
        verification_plan="run syntax checks and compare service behavior",
        rollback_plan="restore the verified timestamped backup",
    )


def _seed_approval(
    client: TestClient,
    *,
    approval_id_target: str,
    now: datetime | None = None,
    linked: bool = False,
    changeset_id: str | None = None,
) -> str:
    runtime = client.app.state.runtime
    if changeset_id is not None:
        _seed_changeset(
            runtime,
            changeset_id=changeset_id,
            target_id=approval_id_target,
            service="payment-api",
        )
    record = asyncio.run(
        runtime.approvals.request(
            {
                "kind": "change",
                "target_id": approval_id_target,
                "service": "payment-api",
                "argv": ["docker", "restart", "payments-api-1"],
                "changeset_id": changeset_id,
            },
            now=now or datetime.now(UTC),
            target_id=approval_id_target,
            service="payment-api",
            investigation_id="inv-1" if linked else None,
            agent_run_id="run-1" if linked else None,
            changeset_id=changeset_id,
            preview={
                "preview": "Protected change requires explicit review.",
                "impact": "May restart the service after configuration changes.",
            },
        )
    )
    return record.approval_id


def test_list_returns_safe_paginated_page(client: TestClient) -> None:
    first = _seed_approval(client, approval_id_target="tgt-a", changeset_id="chs-1")
    second = _seed_approval(client, approval_id_target="tgt-a", changeset_id="chs-2")

    response = client.get(
        ROUTE,
        params={"status": "pending", "limit": 1},
        headers=_auth(OPERATOR_A_TOKEN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_more"] is True
    assert len(body["items"]) == 1
    assert body["items"][0]["approval_id"] == first
    assert body["items"][0]["diff"]
    assert body["items"][0]["verification"] == "run syntax checks and compare service behavior"
    assert body["items"][0]["rollback"] == "restore the verified timestamped backup"
    assert "argv" not in response.text
    assert "intent_sha256" not in response.text
    assert "secret" not in response.text

    second_page = client.get(
        ROUTE,
        params={"after": body["next_cursor"], "limit": 1},
        headers=_auth(OPERATOR_A_TOKEN),
    )
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["approval_id"] == second


def test_list_filters_to_authorized_targets(client: TestClient) -> None:
    _seed_approval(client, approval_id_target="tgt-a")
    visible = _seed_approval(client, approval_id_target="tgt-b")

    response = client.get(ROUTE, headers=_auth(SCOPED_B_TOKEN))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["approval_id"] for item in items] == [visible]


def test_get_and_decide_hide_unauthorized_target(client: TestClient) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a")

    get_response = client.get(f"{ROUTE}/{approval_id}", headers=_auth(SCOPED_B_TOKEN))
    assert get_response.status_code == 404
    assert get_response.json()["error"]["code"] == "resource_not_found"

    decide_response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(SCOPED_B_TOKEN, "approve-hidden"),
        json={"reason": "Looks fine."},
    )
    assert decide_response.status_code == 404
    assert decide_response.json()["error"]["code"] == "resource_not_found"


def test_approve_requires_reason_and_scope(client: TestClient) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a")

    missing_reason = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-missing"),
        json={},
    )
    assert missing_reason.status_code == 422
    assert missing_reason.json()["error"]["code"] == "request_validation_failed"

    read_only = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(READ_ONLY_TOKEN, "approve-readonly"),
        json={"reason": "Approved."},
    )
    assert read_only.status_code == 403
    assert read_only.json()["error"]["code"] == "permission_denied"


def test_body_actor_is_rejected(client: TestClient) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a")
    response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-extra-field"),
        json={"reason": "Approved.", "decided_by": "admin"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_approve_is_idempotent_and_persists_actor_reason(client: TestClient) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a", linked=True)
    headers = _idem(OPERATOR_A_TOKEN, "approve-1")

    first = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=headers,
        json={"reason": "Reviewed exact diff and rollback plan."},
    )
    assert first.status_code == 200, first.text
    assert first.json()["decision_status"] == "approved"
    assert first.json()["decided_by"] == "operator-a"
    assert first.json()["decision_reason"] == "Reviewed exact diff and rollback plan."

    replay = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=headers,
        json={"reason": "Reviewed exact diff and rollback plan."},
    )
    assert replay.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"


def test_contradictory_decision_is_conflict(client: TestClient) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a")
    approved = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-2"),
        json={"reason": "Approved."},
    )
    assert approved.status_code == 200

    rejected = client.post(
        f"{ROUTE}/{approval_id}/reject",
        headers=_idem(OPERATOR_A_TOKEN, "reject-1"),
        json={"reason": "Actually reject."},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "approval_already_decided"


def test_expired_approval_is_conflict(client: TestClient) -> None:
    approval_id = _seed_approval(
        client,
        approval_id_target="tgt-a",
        now=datetime.now(UTC) - timedelta(minutes=16),
    )
    response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-expired"),
        json={"reason": "Too late."},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_expired"


def test_decision_remains_committed_when_downstream_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a", linked=True)
    runtime = client.app.state.runtime

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("resume failed")

    monkeypatch.setattr(runtime.investigations, "handle_approval_decision", _boom)

    response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-downstream-fail"),
        json={"reason": "Reviewed exact diff and rollback plan."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision_status"] == "approved"
    assert body["decided_by"] == "operator-a"
    assert body["downstream_status"] == "failed"
    assert body["downstream_error_code"] == "internal_error"

    stored = runtime.approvals.get(approval_id)
    assert stored is not None
    assert stored.status.value == "approved"
