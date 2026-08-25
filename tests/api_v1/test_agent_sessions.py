from __future__ import annotations

from auth.helpers import AUTH_HEADERS
from fastapi.testclient import TestClient

TARGET = {
    "name": "Payments",
    "host": "payments.example.test",
    "ssh_user": "deploy",
    "ssh_port": 2222,
    "authentication_ref": "ssh-agent:deploy@payments.example.test",
}


def headers(client: TestClient, key: str) -> dict[str, str]:
    return {**AUTH_HEADERS, "Idempotency-Key": key}


def test_message_is_accepted_without_waiting_for_agent_completion(
    authenticated_client: TestClient,
) -> None:
    target = authenticated_client.post(
        "/api/v1/targets", json=TARGET, headers=headers(authenticated_client, "target-1")
    )
    assert target.status_code == 201, target.text
    session = authenticated_client.post(
        "/api/v1/agent-sessions",
        json={"target_id": target.json()["target_id"]},
        headers=headers(authenticated_client, "session-1"),
    )
    assert session.status_code == 201, session.text

    response = authenticated_client.post(
        f"/api/v1/agent-sessions/{session.json()['session_id']}/messages",
        json={"content": "调查 payment-service 频繁重启"},
        headers=headers(authenticated_client, "message-1"),
    )
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] is True
    assert response.json()["operation_id"].startswith("op_")
