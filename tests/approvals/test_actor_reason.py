import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from incidentlens_control_plane.approvals.service import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalService,
)
from incidentlens_control_plane.approvals.types import (
    ApprovalDownstreamStatus,
    ApprovalStatus,
)

INTENT = {
    "kind": "docker.restart",
    "target_id": "dev-a",
    "container": "payments-api-1",
    "argv": ["docker", "restart", "payments-api-1"],
}


def test_approve_persists_actor_reason_and_linkage(
    approval_service: ApprovalService,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        request = await approval_service.request(
            INTENT,
            now=now,
            target_id="dev-a",
            service="payment-api",
            session_id="sess-1",
            investigation_id="inv-1",
            agent_run_id="run-1",
            tool_call_id="call-1",
            changeset_id="chs-1",
            risk="approval_required",
            preview={"preview": "Protected action requires review."},
        )

        approved = await approval_service.approve(
            request.approval_id,
            now=now + timedelta(minutes=1),
            actor="operator-a",
            reason="Reviewed exact diff and rollback plan.",
        )

        assert approved.status is ApprovalStatus.APPROVED
        assert approved.decision_actor == "operator-a"
        assert approved.decision_reason == "Reviewed exact diff and rollback plan."
        assert approved.session_id == "sess-1"
        assert approved.investigation_id == "inv-1"
        assert approved.agent_run_id == "run-1"
        assert approved.tool_call_id == "call-1"
        assert approved.changeset_id == "chs-1"
        assert approved.preview["preview"] == "Protected action requires review."
        assert approved.downstream_status is ApprovalDownstreamStatus.PENDING

    asyncio.run(scenario())


def test_expired_approval_cannot_be_decided(
    approval_service: ApprovalService,
) -> None:
    async def scenario() -> None:
        created = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        request = await approval_service.request(INTENT, now=created)
        with pytest.raises(ApprovalExpired):
            await approval_service.approve(
                request.approval_id,
                now=created + timedelta(minutes=16),
                actor="operator-a",
                reason="Too late.",
            )

    asyncio.run(scenario())


def test_contradictory_decision_raises_already_decided(
    approval_service: ApprovalService,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        request = await approval_service.request(INTENT, now=now)
        await approval_service.approve(
            request.approval_id,
            now=now,
            actor="operator-a",
            reason="Approved.",
        )
        with pytest.raises(ApprovalAlreadyDecided):
            await approval_service.reject(
                request.approval_id,
                now=now + timedelta(seconds=1),
                actor="operator-b",
                reason="Actually no.",
            )

    asyncio.run(scenario())
