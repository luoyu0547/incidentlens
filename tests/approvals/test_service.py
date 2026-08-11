import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from incidentlens_control_plane.approvals.service import (
    ApprovalMismatch,
    ApprovalService,
    ApprovalUnavailable,
)

INTENT = {
    "kind": "docker.restart",
    "target_id": "dev-a",
    "container": "payments-api-1",
    "argv": ["docker", "restart", "payments-api-1"],
}


def test_approval_is_bound_to_canonical_parameters(approval_service: ApprovalService) -> None:
    async def scenario() -> None:
        intent = {
            "kind": "docker.restart",
            "target_id": "dev-a",
            "container": "payments-api-1",
            "argv": ["docker", "restart", "payments-api-1"],
        }
        now = datetime(2026, 8, 10, tzinfo=UTC)
        request = await approval_service.request(intent, now=now)
        approved = await approval_service.approve(
            request.approval_id, now=request.created_at
        )

        with pytest.raises(ApprovalMismatch):
            await approval_service.consume(
                approved.approval_id,
                {**intent, "container": "other"},
                now=now,
            )

        await approval_service.consume(approved.approval_id, intent, now=now)
        with pytest.raises(ApprovalUnavailable):
            await approval_service.consume(approved.approval_id, intent, now=now)

    asyncio.run(scenario())


def test_expired_approval_cannot_be_consumed(approval_service: ApprovalService) -> None:
    async def scenario() -> None:
        created = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        request = await approval_service.request(INTENT, now=created)
        await approval_service.approve(request.approval_id, now=created)
        with pytest.raises(ApprovalUnavailable):
            await approval_service.consume(
                request.approval_id,
                INTENT,
                now=created + timedelta(minutes=16),
            )

    asyncio.run(scenario())
