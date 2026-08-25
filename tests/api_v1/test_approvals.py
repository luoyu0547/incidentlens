from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.api.idempotency import idempotency_request_sha256
from incidentlens_control_plane.changes.types import FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.service import ApprovalDecisionOutcome
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
from incidentlens_control_plane.remote_ops.gateway import Gateway
from incidentlens_control_plane.remote_ops.types import HostScope, OperationRisk, ShellRequest
from incidentlens_control_plane.targets.types import TargetBinding

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


def _seed_sensitive_changeset(
    runtime, *, changeset_id: str, target_id: str, service: str
) -> None:
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
                expected_sha256="c" * 64,
                replacement_sha256="d" * 64,
                diff_text=(
                    "-DATABASE_URL=postgresql://alice:hunter2@db/prod\n"
                    "+DATABASE_URL=postgresql://alice:[REDACTED]@db/prod\n"
                ),
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
    project_id: str | None = None,
    now: datetime | None = None,
    linked: bool = False,
    changeset_id: str | None = None,
    seed_changeset: bool = True,
) -> str:
    runtime = client.app.state.runtime
    if changeset_id is not None and seed_changeset:
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
            project_id=project_id,
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
    assert "file change(s)" in body["items"][0]["diff"]
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


def test_detail_never_serializes_raw_diff_credentials(client: TestClient) -> None:
    runtime = client.app.state.runtime
    _seed_sensitive_changeset(
        runtime,
        changeset_id="chs-sensitive",
        target_id="tgt-a",
        service="payment-api",
    )
    approval_id = _seed_approval(
        client,
        approval_id_target="tgt-a",
        changeset_id="chs-sensitive",
        seed_changeset=False,
    )

    response = client.get(f"{ROUTE}/{approval_id}", headers=_auth(OPERATOR_A_TOKEN))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diff"] == (
        "1 file change(s); scopes=1×host; local_backups=1; "
        "remote_backups=1; checksum_pairs=1"
    )
    assert "DATABASE_URL" not in response.text
    assert "postgresql://" not in response.text
    assert "alice" not in response.text
    assert "hunter2" not in response.text


def test_list_filters_to_authorized_targets(client: TestClient) -> None:
    _seed_approval(client, approval_id_target="tgt-a")
    visible = _seed_approval(client, approval_id_target="tgt-b")

    response = client.get(ROUTE, headers=_auth(SCOPED_B_TOKEN))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["approval_id"] for item in items] == [visible]


def test_list_get_and_decide_resolve_facade_target_authorization(client: TestClient) -> None:
    runtime = client.app.state.runtime
    now = datetime.now(UTC)
    runtime.target_store.create(
        TargetBinding(
            target_id="tgt-a-collision",
            project_id="proj-a",
            registry_target_id="default",
            name="Project A",
            authentication_ref="",
            host_key_policy="strict",
            pinned_host_key_sha256=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    runtime.target_store.create(
        TargetBinding(
            target_id="tgt-b",
            project_id="proj-b",
            registry_target_id="default",
            name="Project B",
            authentication_ref="",
            host_key_policy="strict",
            pinned_host_key_sha256=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    hidden = _seed_approval(
        client,
        approval_id_target="default",
        project_id="proj-a",
    )
    visible = _seed_approval(
        client,
        approval_id_target="default",
        project_id="proj-b",
    )

    list_response = client.get(ROUTE, headers=_auth(SCOPED_B_TOKEN))
    assert list_response.status_code == 200, list_response.text
    assert [item["approval_id"] for item in list_response.json()["items"]] == [visible]
    assert list_response.json()["items"][0]["linkage"]["target_id"] == "tgt-b"

    get_visible = client.get(f"{ROUTE}/{visible}", headers=_auth(SCOPED_B_TOKEN))
    assert get_visible.status_code == 200
    assert get_visible.json()["linkage"]["target_id"] == "tgt-b"

    get_hidden = client.get(f"{ROUTE}/{hidden}", headers=_auth(SCOPED_B_TOKEN))
    assert get_hidden.status_code == 404
    assert get_hidden.json()["error"]["code"] == "resource_not_found"

    decide_hidden = client.post(
        f"{ROUTE}/{hidden}/approve",
        headers=_idem(SCOPED_B_TOKEN, "approve-hidden-collision"),
        json={"reason": "Looks fine."},
    )
    assert decide_hidden.status_code == 404
    assert decide_hidden.json()["error"]["code"] == "resource_not_found"


def test_missing_facade_binding_fails_closed_for_list_get_and_decision(
    client: TestClient,
) -> None:
    orphan = _seed_approval(
        client,
        approval_id_target="default",
        project_id="proj-orphan",
    )
    visible = _seed_approval(client, approval_id_target="tgt-a")

    list_response = client.get(ROUTE, headers=_auth(OPERATOR_A_TOKEN))
    assert list_response.status_code == 200, list_response.text
    assert [item["approval_id"] for item in list_response.json()["items"]] == [visible]

    get_response = client.get(f"{ROUTE}/{orphan}", headers=_auth(OPERATOR_A_TOKEN))
    assert get_response.status_code == 404
    assert get_response.json()["error"]["code"] == "resource_not_found"

    decide_response = client.post(
        f"{ROUTE}/{orphan}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-orphan"),
        json={"reason": "Approved."},
    )
    assert decide_response.status_code == 404
    assert decide_response.json()["error"]["code"] == "resource_not_found"


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


def test_list_rejects_timezone_naive_cursor_created_at(client: TestClient) -> None:
    first = _seed_approval(client, approval_id_target="tgt-a")
    _seed_approval(client, approval_id_target="tgt-a")

    first_page = client.get(
        ROUTE,
        params={"status": "pending", "limit": 1},
        headers=_auth(OPERATOR_A_TOKEN),
    )
    assert first_page.status_code == 200, first_page.text
    payload = first_page.json()
    assert payload["items"][0]["approval_id"] == first
    encoded = payload["next_cursor"]
    assert encoded is not None
    raw = json.loads(base64.urlsafe_b64decode(encoded[4:].encode("ascii")).decode("utf-8"))
    raw["created_at"] = raw["created_at"].replace("+00:00", "")
    naive_cursor = "ap1_" + base64.urlsafe_b64encode(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    response = client.get(
        ROUTE,
        params={"after": naive_cursor, "limit": 1},
        headers=_auth(OPERATOR_A_TOKEN),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "cursor_invalid"


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


def test_shell_gateway_created_approval_preserves_linkage_for_v1_decision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = client.app.state.runtime
    now = datetime.now(UTC)
    runtime.target_store.create(
        TargetBinding(
            target_id="tgt-a",
            project_id="proj-shell",
            registry_target_id="tgt-a",
            name="Shell Target",
            authentication_ref="",
            host_key_policy="strict",
            pinned_host_key_sha256=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    request = ShellRequest(
        operation_id="call-1",
        incident_id="inc-1",
        project_id="proj-shell",
        target_id="tgt-a",
        service="payment-api",
        scope=HostScope(),
        session_id="sess-1",
        investigation_id="inv-1",
        agent_run_id="run-1",
        tool_call_id="call-1",
        risk=OperationRisk.APPROVAL_REQUIRED,
        command="systemctl restart payments",
        reason="restart the service",
    )

    result = asyncio.run(Gateway(runtime.approvals).shell(request))
    assert result.approved is False
    approval_id = result.approval_id
    assert approval_id is not None

    calls: list[str] = []

    async def _resume(*args, **kwargs):
        calls.append(kwargs.get("approval_id", args[0] if args else ""))
        return ApprovalDecisionOutcome(
            approval_id=approval_id,
            matched="tool_call",
            tool_call_id="call-1",
            run_id="run-1",
            action="re-executed",
            applied=True,
            consumed=True,
        )

    monkeypatch.setattr(runtime.investigations, "handle_approval_decision", _resume)

    response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-shell-linkage"),
        json={"reason": "Approved exact shell action."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["downstream_status"] == "processed"
    assert body["linkage"] == {
        "target_id": "tgt-a",
        "service": "payment-api",
        "session_id": "sess-1",
        "investigation_id": "inv-1",
        "agent_run_id": "run-1",
        "tool_call_id": "call-1",
        "changeset_id": None,
        "proposal_id": None,
    }
    assert calls == [approval_id]

    stored = runtime.approvals.get(approval_id)
    assert stored is not None
    assert stored.project_id == "proj-shell"
    assert stored.session_id == "sess-1"
    assert stored.investigation_id == "inv-1"
    assert stored.agent_run_id == "run-1"
    assert stored.tool_call_id == "call-1"


def test_same_key_retry_resumes_matching_decision_after_crash_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a", linked=True)
    runtime = client.app.state.runtime
    reason = "Reviewed exact diff and rollback plan."
    seeded = runtime.approvals.get(approval_id)
    assert seeded is not None
    decision_now = seeded.created_at + timedelta(minutes=1)
    request_body = json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":"))
    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key="/api/v1/approvals/{approval_id}/approve",
        path_params={"approval_id": approval_id},
        canonical_body=request_body,
    )

    asyncio.run(
        runtime.approvals.approve(
            approval_id,
            now=decision_now,
            actor="operator-a",
            reason=reason,
            route_key="/api/v1/approvals/{approval_id}/approve",
            idempotency_key="approve-retry",
            request_sha256=request_sha256,
        )
    )
    runtime.idempotency.reserve(
        principal_id="operator-a",
        method="POST",
        route_key="/api/v1/approvals/{approval_id}/approve",
        idempotency_key="approve-retry",
        request_sha256=request_sha256,
        now=decision_now - timedelta(minutes=10),
    )

    calls: list[str] = []

    async def _resume(*args, **kwargs):
        calls.append(kwargs.get("approval_id", args[0] if args else ""))
        return ApprovalDecisionOutcome(
            approval_id=approval_id,
            matched="tool_call",
            tool_call_id="call-1",
            run_id="run-1",
            action="re-executed",
            applied=True,
            consumed=True,
        )

    monkeypatch.setattr(runtime.investigations, "handle_approval_decision", _resume)

    response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-retry"),
        json={"reason": reason},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision_status"] == "approved"
    assert body["downstream_status"] == "processed"
    assert body["decided_by"] == "operator-a"
    assert calls == [approval_id]

    stored = runtime.approvals.get(approval_id)
    assert stored is not None
    assert stored.downstream_status.value == "processed"

    replay = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-retry"),
        json={"reason": reason},
    )
    assert replay.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"


def test_fresh_key_cannot_resume_matching_decision_after_crash_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _seed_approval(client, approval_id_target="tgt-a", linked=True)
    runtime = client.app.state.runtime
    reason = "Reviewed exact diff and rollback plan."
    seeded = runtime.approvals.get(approval_id)
    assert seeded is not None
    decision_now = seeded.created_at + timedelta(minutes=1)
    request_body = json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":"))
    request_sha256 = idempotency_request_sha256(
        method="POST",
        route_key="/api/v1/approvals/{approval_id}/approve",
        path_params={"approval_id": approval_id},
        canonical_body=request_body,
    )

    asyncio.run(
        runtime.approvals.approve(
            approval_id,
            now=decision_now,
            actor="operator-a",
            reason=reason,
            route_key="/api/v1/approvals/{approval_id}/approve",
            idempotency_key="approve-original",
            request_sha256=request_sha256,
        )
    )

    calls: list[str] = []

    async def _resume(*args, **kwargs):
        calls.append(kwargs.get("approval_id", args[0] if args else ""))
        return ApprovalDecisionOutcome(
            approval_id=approval_id,
            matched="tool_call",
            tool_call_id="call-1",
            run_id="run-1",
            action="re-executed",
            applied=True,
            consumed=True,
        )

    monkeypatch.setattr(runtime.investigations, "handle_approval_decision", _resume)

    response = client.post(
        f"{ROUTE}/{approval_id}/approve",
        headers=_idem(OPERATOR_A_TOKEN, "approve-fresh"),
        json={"reason": reason},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_already_decided"
    assert calls == []
