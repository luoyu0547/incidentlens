"""End-to-end tests for the Target product facade over ``/api/v1/targets``.

These exercise the authenticated v1 surface (shared ``authenticated_client``
fixture), the idempotency machinery on mutations, the stable error envelope, and
the ProjectRegistry-backed read projections.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)

ROUTE = "/api/v1/targets"

PROJECT_PAYLOAD = {
    "project_id": "payments",
    "display_name": "Payments",
    "local_source_paths": ["/srv/payments"],
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
            "local_source_path": "/srv/payments",
            "allowed_host_paths": ["/opt/payments"],
            "protected_remote_paths": ["/opt/payments/.env"],
        }
    ],
}

CREATE_PAYLOAD = {
    "name": "Payments",
    "host": "payments.example.test",
    "ssh_user": "deploy",
    "ssh_port": 2222,
    "authentication_ref": "ssh-agent:deploy@payments.example.test",
}


def _headers(client: TestClient, key: str) -> dict[str, str]:
    """Bearer-authenticated headers carrying an ``Idempotency-Key``.

    The bearer path is CSRF-exempt by construction, matching the conftest
    idempotency route.
    """
    return {"Idempotency-Key": key, **client.AUTH_HEADERS}


def _seed_project(client: TestClient, payload: dict[str, object]) -> None:
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text


def test_create_target_never_leaks_auth_ref_and_replays(
    authenticated_client: TestClient,
) -> None:
    headers = _headers(authenticated_client, "target-create-1")
    response = authenticated_client.post(ROUTE, json=CREATE_PAYLOAD, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Payments"
    assert body["host"] == "payments.example.test"
    assert body["ssh_port"] == 2222
    assert body["version"] == 1
    assert body["authentication_configured"] is True
    assert body["authentication_hint"] == "ssh-agent"
    assert "authentication_ref" not in body
    assert "ssh-agent:deploy@payments" not in response.text

    replay = authenticated_client.post(ROUTE, json=CREATE_PAYLOAD, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["target_id"] == body["target_id"]
    assert replay.headers.get("Idempotency-Replayed") == "true"


def test_list_and_get_include_created_target(
    authenticated_client: TestClient,
) -> None:
    created = authenticated_client.post(
        ROUTE, json=CREATE_PAYLOAD, headers=_headers(authenticated_client, "create-2")
    ).json()
    listed = authenticated_client.get(ROUTE)
    assert listed.status_code == 200
    ids = [target["target_id"] for target in listed.json()]
    assert created["target_id"] in ids

    fetched = authenticated_client.get(f"{ROUTE}/{created['target_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_patch_requires_idempotency_key(authenticated_client: TestClient) -> None:
    created = authenticated_client.post(
        ROUTE, json=CREATE_PAYLOAD, headers=_headers(authenticated_client, "create-3")
    ).json()
    missing = authenticated_client.patch(
        f"{ROUTE}/{created['target_id']}",
        json={"name": "Nope", "expected_version": 1},
        headers=authenticated_client.AUTH_HEADERS,
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "idempotency_key_required"


def test_patch_stale_version_is_resource_conflict(
    authenticated_client: TestClient,
) -> None:
    created = authenticated_client.post(
        ROUTE, json=CREATE_PAYLOAD, headers=_headers(authenticated_client, "create-4")
    ).json()
    okay = authenticated_client.patch(
        f"{ROUTE}/{created['target_id']}",
        json={"name": "Bumped", "expected_version": 1},
        headers=_headers(authenticated_client, "patch-ok"),
    )
    assert okay.status_code == 200
    assert okay.json()["version"] == 2
    stale = authenticated_client.patch(
        f"{ROUTE}/{created['target_id']}",
        json={"name": "Stale", "expected_version": 1},
        headers=_headers(authenticated_client, "patch-stale"),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "resource_conflict"


def test_patch_preserves_services_and_services_route(
    authenticated_client: TestClient,
) -> None:
    _seed_project(authenticated_client, PROJECT_PAYLOAD)

    bound = authenticated_client.get(f"{ROUTE}/dev-a")
    assert bound.status_code == 200
    assert bound.json()["target_id"] == "dev-a"
    assert bound.json()["version"] == 1
    assert bound.json()["authentication_configured"] is False

    patched = authenticated_client.patch(
        f"{ROUTE}/dev-a",
        json={"name": "Payments API", "host": "dev-b.example.test", "expected_version": 1},
        headers=_headers(authenticated_client, "patch-dev-a"),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["host"] == "dev-b.example.test"
    assert patched.json()["name"] == "Payments API"
    assert patched.json()["version"] == 2

    services = authenticated_client.get(f"{ROUTE}/dev-a/services")
    assert services.status_code == 200
    assert services.json() == [
        {
            "service": "payment-api",
            "container_names": ["payments-api-1"],
            "allowed_host_paths": ["/opt/payments"],
            "protected_remote_paths": ["/opt/payments/.env"],
        }
    ]


def test_duplicate_internal_target_ids_do_not_alias(
    authenticated_client: TestClient,
) -> None:
    _seed_project(
        authenticated_client,
        {
            "project_id": "payments",
            "display_name": "Payments",
            "local_source_paths": ["/srv/payments"],
            "targets": [{"target_id": "db", "host": "db-a", "ssh_user": "deploy"}],
        },
    )
    _seed_project(
        authenticated_client,
        {
            "project_id": "analytics",
            "display_name": "Analytics",
            "local_source_paths": ["/srv/analytics"],
            "targets": [{"target_id": "db", "host": "db-b", "ssh_user": "deploy"}],
        },
    )
    listed = authenticated_client.get(ROUTE)
    assert listed.status_code == 200
    targets = listed.json()
    assert len(targets) == 2
    ids = {target["target_id"] for target in targets}
    assert len(ids) == 2
    assert "db" not in ids
    by_host = {target["host"]: target["target_id"] for target in targets}
    assert by_host["db-a"] != by_host["db-b"]

    assert authenticated_client.get(f"{ROUTE}/{by_host['db-a']}").json()["host"] == "db-a"
    assert authenticated_client.get(f"{ROUTE}/{by_host['db-b']}").json()["host"] == "db-b"


def test_delete_blocked_by_active_investigation_then_succeeds(
    authenticated_client: TestClient,
) -> None:
    created = authenticated_client.post(
        ROUTE, json=CREATE_PAYLOAD, headers=_headers(authenticated_client, "create-5")
    ).json()
    target_id = created["target_id"]
    runtime = authenticated_client.app.state.runtime
    binding = runtime.target_store.get(target_id)

    now = datetime.now(UTC)
    investigation = Investigation(
        investigation_id="inv-target-1",
        incident_id="inc-target-1",
        project_id=binding.project_id,
        target_id=binding.registry_target_id,
        service="default",
        symptom="down",
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=now,
        updated_at=now,
    )
    runtime.investigation_store.create_investigation(investigation)

    blocked = authenticated_client.delete(
        f"{ROUTE}/{target_id}", headers=_headers(authenticated_client, "delete-1")
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "resource_conflict"

    runtime.investigation_store.transition_investigation_status(
        investigation.investigation_id,
        InvestigationStatus.COMPLETED,
        now=now,
    )

    deleted = authenticated_client.delete(
        f"{ROUTE}/{target_id}", headers=_headers(authenticated_client, "delete-2")
    )
    assert deleted.status_code == 204, deleted.text
    assert authenticated_client.get(f"{ROUTE}/{target_id}").status_code == 404


def test_delete_replays_idempotently(authenticated_client: TestClient) -> None:
    created = authenticated_client.post(
        ROUTE, json=CREATE_PAYLOAD, headers=_headers(authenticated_client, "create-6")
    ).json()
    target_id = created["target_id"]
    headers = _headers(authenticated_client, "delete-replay")
    first = authenticated_client.delete(f"{ROUTE}/{target_id}", headers=headers)
    second = authenticated_client.delete(f"{ROUTE}/{target_id}", headers=headers)
    assert first.status_code == 204
    assert second.status_code == 204
    assert second.headers.get("Idempotency-Replayed") == "true"


def test_missing_idempotency_key_on_create_is_422(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        ROUTE, json=CREATE_PAYLOAD, headers=authenticated_client.AUTH_HEADERS
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_actor_body_fields_are_rejected(authenticated_client: TestClient) -> None:
    payload = {**CREATE_PAYLOAD, "created_by": "operator-a"}
    response = authenticated_client.post(
        ROUTE, json=payload, headers=_headers(authenticated_client, "actor-fields-1")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_unauthenticated_request_is_401(client: TestClient) -> None:
    response = client.get(ROUTE)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
