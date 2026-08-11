"""Tests for approval decision HTTP API."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_approval_decision_is_single_use(
    client: TestClient, pending_approval: str
) -> None:
    approved = client.post(f"/api/approvals/{pending_approval}/approve")
    repeated = client.post(f"/api/approvals/{pending_approval}/approve")
    assert approved.status_code == 200
    assert repeated.status_code == 409


def test_approval_reject_is_single_use(
    client: TestClient, pending_approval: str
) -> None:
    rejected = client.post(f"/api/approvals/{pending_approval}/reject")
    repeated = client.post(f"/api/approvals/{pending_approval}/reject")
    assert rejected.status_code == 200
    assert repeated.status_code == 409


def test_approvals_list_filters_by_status(
    client: TestClient, pending_approval: str
) -> None:
    response = client.get("/api/approvals", params={"status": "pending"})
    assert response.status_code == 200
    items = response.json()
    assert any(item["approval_id"] == pending_approval for item in items)
    assert all(item["status"] == "pending" for item in items)


def test_approval_view_excludes_canonical_intent(
    client: TestClient, pending_approval: str
) -> None:
    response = client.get("/api/approvals", params={"status": "pending"})
    assert response.status_code == 200
    assert "argv" not in response.text
    assert "intent_sha256" not in response.text


def test_approve_unknown_approval_returns_409(
    client: TestClient,
) -> None:
    response = client.post("/api/approvals/apr-missing/approve")
    assert response.status_code == 409
