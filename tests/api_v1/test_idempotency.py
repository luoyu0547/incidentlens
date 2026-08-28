"""End-to-end idempotency tests over the TEST-ONLY v1 fixture route.

``POST /api/v1/test-idempotent`` is registered in ``conftest.py`` to exercise
``execute_idempotent`` through the real auth dependency stack: create returns
201, a same-key + same-body retry replays the exact 2xx with
``Idempotency-Replayed: true``, and a same-key + different-body retry is a 409
``idempotency_conflict``.  Missing/malformed keys are 422
``idempotency_key_required``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

ROUTE = "/api/v1/test-idempotent"


def _headers(client: TestClient, key: str) -> dict[str, str]:
    """Bearer-authenticated headers carrying an ``Idempotency-Key``.

    The bearer path is CSRF-exempt by construction, matching the brief's
    requirement that requests not need a manually attached CSRF token.
    """
    return {"Idempotency-Key": key, **client.AUTH_HEADERS}


def test_first_execution_returns_201(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(
        ROUTE, headers=_headers(authenticated_client, "target-create-1"),
        json={"value": "a"},
    )
    assert response.status_code == 201
    assert response.json() == {"value": "a", "created": True}
    assert "Idempotency-Replayed" not in response.headers


def test_same_key_different_request_is_conflict(
    authenticated_client: TestClient,
) -> None:
    key = "target-create-1"
    first = authenticated_client.post(
        ROUTE, headers=_headers(authenticated_client, key), json={"value": "a"}
    )
    second = authenticated_client.post(
        ROUTE, headers=_headers(authenticated_client, key), json={"value": "b"}
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_same_key_same_body_replays_exact_response(
    authenticated_client: TestClient,
) -> None:
    key = "target-create-2"
    first = authenticated_client.post(
        ROUTE, headers=_headers(authenticated_client, key), json={"value": "a"}
    )
    second = authenticated_client.post(
        ROUTE, headers=_headers(authenticated_client, key), json={"value": "a"}
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert first.headers.get("Idempotency-Replayed") is None
    assert second.headers.get("Idempotency-Replayed") == "true"


def test_cookie_csrf_path_also_works(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        ROUTE,
        headers={
            "Idempotency-Key": "cookie-auth-1",
            "X-CSRF-Token": authenticated_client.csrf,
        },
        json={"value": "a"},
    )
    assert response.status_code == 201
    assert response.json() == {"value": "a", "created": True}


def test_missing_key_is_422_idempotency_key_required(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        ROUTE, headers=authenticated_client.AUTH_HEADERS, json={"value": "a"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_malformed_key_is_422_idempotency_key_required(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        ROUTE,
        headers={
            "Idempotency-Key": "bad key!",
            **authenticated_client.AUTH_HEADERS,
        },
        json={"value": "a"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_unknown_body_field_is_422_request_validation_failed(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        ROUTE,
        headers=_headers(authenticated_client, "strict-body-1"),
        json={"value": "a", "unexpected": True},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_in_progress_reservation_returns_409_with_retry_after(
    authenticated_client: TestClient,
) -> None:
    key = "inflight-1"
    # Seed an unexpired in_progress reservation through the runtime store so
    # the request observes a still-running sibling.
    authenticated_client.app.state.runtime.idempotency.reserve(
        principal_id="operator-a",
        method="POST",
        route_key=ROUTE,
        idempotency_key=key,
        request_sha256="x" * 64,
        now=datetime.now(UTC),
    )
    response = authenticated_client.post(
        ROUTE, headers=_headers(authenticated_client, key), json={"value": "a"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_in_progress"
    assert response.headers["Retry-After"] == "1"


def test_unauthenticated_request_is_401(authenticated_client: TestClient) -> None:
    authenticated_client.cookies.clear()
    response = authenticated_client.post(
        ROUTE, headers={"Idempotency-Key": "anon-1"}, json={"value": "a"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
