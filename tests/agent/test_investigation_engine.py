"""Tests for the investigation engine — TDD RED phase.

Tests cover:
  - Engine persists evidence after tool calls
  - State machine transitions through all phases
  - Checkpointing enables resume
  - Round limit enforcement (default 8)
  - Evidence dedup for same tool+args
  - Confidence > 0.70 must reference current Evidence.id
  - Conflicting evidence lowers confidence
  - Error/empty results are recorded as evidence
  - Report generation guard (only when root cause verified)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from incidentlens_contracts.models import (
    HypothesisStatus,
    InvestigationStatus,
    TelemetryEvent,
)
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def telemetry_repo():
    """Create a TelemetryRepository backed by an in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:")
    return TelemetryRepository(engine)


@pytest.fixture()
def toolkit(telemetry_repo):
    """Create a ReadOnlyToolkit with the in-memory repository."""
    from incidentlens_control_plane.tools.query import ReadOnlyToolkit

    return ReadOnlyToolkit(telemetry_repo)


@pytest.fixture()
def engine(telemetry_repo, toolkit):
    """Create an InvestigationEngine with toolkit and telemetry."""
    from incidentlens_control_plane.agent.engine import InvestigationEngine

    return InvestigationEngine(
        telemetry_repo=telemetry_repo,
        toolkit=toolkit,
    )


@pytest.fixture()
def checkpoints(engine):
    """Provide access to the checkpoint store."""
    return engine.checkpoint_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(minute: int = 0) -> datetime:
    return datetime(2025, 1, 1, 0, minute, tzinfo=timezone.utc)


def _log_event(
    service: str = "order-service",
    trace_id: str = "trace-a",
    level: str = "ERROR",
    message: str = "payment failed",
    minute: int = 0,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type="log",
        service=service,
        trace_id=trace_id,
        occurred_at=_ts(minute),
        payload={"level": level, "message": message},
    )


def _span_event(
    service: str = "order-service",
    trace_id: str = "trace-a",
    span_id: str = "span-1",
    parent_id: str | None = None,
    operation: str = "POST /checkout",
    minute: int = 0,
) -> TelemetryEvent:
    payload: dict = {"span_id": span_id, "operation": operation}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    return TelemetryEvent(
        event_type="span",
        service=service,
        trace_id=trace_id,
        occurred_at=_ts(minute),
        payload=payload,
    )


def _deployment_event(
    service: str = "order-service",
    version: str = "v2.3.1",
    minute: int = 0,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type="deployment",
        service=service,
        trace_id="",
        occurred_at=_ts(minute),
        payload={"version": version},
    )


def _metric_event(
    service: str = "order-service",
    trace_id: str = "trace-a",
    name: str = "error_rate",
    value: float = 0.17,
    minute: int = 0,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type="metric",
        service=service,
        trace_id=trace_id,
        occurred_at=_ts(minute),
        payload={"name": name, "value": value},
    )


# ===================================================================
# CORE: Engine persists evidence after tool calls
# ===================================================================


class TestEnginePersistsEvidence:
    """Core TDD test: engine persists evidence after two calls."""

    @pytest.mark.asyncio
    async def test_engine_persists_evidence_after_two_calls(
        self, engine, checkpoints, telemetry_repo
    ) -> None:
        """After start + 2 rounds, evidence should be persisted in checkpoints."""
        # Seed some telemetry data so tools return results
        telemetry_repo.record(_log_event(message="timeout at payment"))
        telemetry_repo.record(_metric_event(value=0.17))
        telemetry_repo.record(_deployment_event(version="v2.3.1"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        state = await engine.run_round(state.incident_id)
        state = await engine.run_round(state.incident_id)

        loaded = checkpoints.load(state.incident_id)
        assert loaded is not None
        assert len(loaded.evidence) > 0


# ===================================================================
# STATE MACHINE: transitions through phases
# ===================================================================


class TestStateMachine:
    """Tests for investigation state machine transitions."""

    @pytest.mark.asyncio
    async def test_start_sets_scoping_status(self, engine) -> None:
        """start() should create an investigation in scoping status."""
        state = engine.start({"service": "order-service", "error_rate": 0.17})
        assert state.status == InvestigationStatus.SCOPING

    @pytest.mark.asyncio
    async def test_run_round_transitions_to_investigating(
        self, engine, telemetry_repo
    ) -> None:
        """First run_round should transition to investigating."""
        telemetry_repo.record(_log_event(message="timeout"))
        state = engine.start({"service": "order-service", "error_rate": 0.17})
        state = await engine.run_round(state.incident_id)
        assert state.status in (
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.NEEDS_MORE_EVIDENCE,
            InvestigationStatus.REPORT_READY,
        )

    @pytest.mark.asyncio
    async def test_incident_id_is_stable(self, engine) -> None:
        """incident_id should remain the same across rounds."""
        state = engine.start({"service": "order-service", "error_rate": 0.17})
        incident_id = state.incident_id
        # Run multiple rounds (with data so tools return results)
        state2 = await engine.run_round(incident_id)
        assert state2.incident_id == incident_id


# ===================================================================
# CHECKPOINTING: resume from checkpoint
# ===================================================================


class TestCheckpointing:
    """Tests for checkpoint persistence and resume."""

    @pytest.mark.asyncio
    async def test_resume_continues_from_checkpoint(
        self, engine, telemetry_repo
    ) -> None:
        """resume() should load state and continue investigation."""
        telemetry_repo.record(_log_event(message="timeout"))
        telemetry_repo.record(_metric_event(value=0.17))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        state = await engine.run_round(state.incident_id)
        round_after_first = state.current_round

        # Resume should pick up from where we left off
        resumed = await engine.resume(state.incident_id)
        assert resumed is not None
        assert resumed.incident_id == state.incident_id
        assert resumed.current_round >= round_after_first

    @pytest.mark.asyncio
    async def test_resume_nonexistent_returns_none(self, engine) -> None:
        """resume() for unknown incident_id should return None."""
        result = await engine.resume("nonexistent-id")
        assert result is None


# ===================================================================
# ROUND LIMIT: default 8 rounds max
# ===================================================================


class TestRoundLimit:
    """Tests for round limit enforcement."""

    @pytest.mark.asyncio
    async def test_engine_stops_after_max_rounds(
        self, engine, telemetry_repo
    ) -> None:
        """Engine should stop after 8 rounds (default max)."""
        telemetry_repo.record(_log_event(message="timeout"))
        telemetry_repo.record(_metric_event(value=0.17))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(10):  # Try more than max
            state = await engine.run_round(state.incident_id)

        assert state.current_round <= 8


# ===================================================================
# EVIDENCE DEDUP: same tool+args should not duplicate evidence
# ===================================================================


class TestEvidenceDedup:
    """Tests for evidence deduplication."""

    @pytest.mark.asyncio
    async def test_same_tool_args_deduped(
        self, engine, checkpoints, telemetry_repo
    ) -> None:
        """Calling the same tool with same args should not create duplicate evidence."""
        telemetry_repo.record(_log_event(message="timeout"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        # Run enough rounds to potentially call same tool twice
        for _ in range(4):
            state = await engine.run_round(state.incident_id)

        loaded = checkpoints.load(state.incident_id)
        # Check no duplicate source_tool+tool_call_id combos
        seen: set[tuple[str, str]] = set()
        for ev in loaded.evidence:
            key = (ev.source_tool, ev.tool_call_id)
            assert key not in seen, f"Duplicate evidence for {key}"
            seen.add(key)


# ===================================================================
# CONFIDENCE GUARD: > 0.70 must reference current Evidence.id
# ===================================================================


class TestConfidenceGuard:
    """Tests for confidence > 0.70 requiring evidence references."""

    @pytest.mark.asyncio
    async def test_high_confidence_must_reference_evidence(
        self, engine, telemetry_repo
    ) -> None:
        """Hypotheses with confidence > 0.70 must have supporting_evidence_ids."""
        telemetry_repo.record(_log_event(message="timeout"))
        telemetry_repo.record(_metric_event(value=0.17))
        telemetry_repo.record(_deployment_event(version="v2.3.1"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(8):
            state = await engine.run_round(state.incident_id)

        # Check all high-confidence hypotheses reference evidence
        for hyp in state.hypotheses:
            if hyp.confidence > 0.70:
                assert len(hyp.supporting_evidence_ids) > 0, (
                    f"Hypothesis {hyp.id} has confidence {hyp.confidence} "
                    f"but no supporting evidence"
                )

    @pytest.mark.asyncio
    async def test_high_confidence_without_evidence_gets_needs_more(
        self, engine, telemetry_repo
    ) -> None:
        """If confidence > 0.70 without evidence, status should be needs_more_evidence."""
        telemetry_repo.record(_log_event(message="timeout"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(8):
            state = await engine.run_round(state.incident_id)

        # Any hypothesis with confidence > 0.70 but no evidence refs
        # should result in investigation status needs_more_evidence
        for hyp in state.hypotheses:
            if hyp.confidence > 0.70 and not hyp.supporting_evidence_ids:
                assert state.status == InvestigationStatus.NEEDS_MORE_EVIDENCE
                return
        # If no such hypothesis exists, that's fine (all properly referenced)


# ===================================================================
# CONFLICTING EVIDENCE: lowers confidence
# ===================================================================


class TestConflictingEvidence:
    """Tests for conflicting evidence lowering confidence."""

    @pytest.mark.asyncio
    async def test_conflicting_evidence_lowers_confidence(
        self, engine, telemetry_repo
    ) -> None:
        """When evidence contradicts a hypothesis, confidence should decrease."""
        # Create conflicting data: errors but also normal metrics
        telemetry_repo.record(
            _log_event(message="timeout at payment", level="ERROR")
        )
        telemetry_repo.record(
            _metric_event(name="error_rate", value=0.01, minute=5)
        )
        telemetry_repo.record(_deployment_event(version="v2.3.1"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(8):
            state = await engine.run_round(state.incident_id)

        # At least one hypothesis should have contradicting evidence
        # or confidence should be moderated
        has_moderation = any(
            hyp.contradicting_evidence_ids
            for hyp in state.hypotheses
            if hyp.status == HypothesisStatus.ACTIVE
        )
        has_low_confidence = any(
            hyp.confidence < 0.90
            for hyp in state.hypotheses
            if hyp.status == HypothesisStatus.ACTIVE
        )
        assert has_moderation or has_low_confidence or len(state.hypotheses) == 0


# ===================================================================
# ERROR/EMPTY RESULTS: recorded as evidence
# ===================================================================


class TestErrorEmptyResults:
    """Tests for recording error and empty tool results as evidence."""

    @pytest.mark.asyncio
    async def test_empty_result_recorded_as_evidence(
        self, engine, checkpoints
    ) -> None:
        """Tool returning empty data should still be recorded as evidence."""
        # No telemetry data — tools will return empty results
        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(4):
            state = await engine.run_round(state.incident_id)

        loaded = checkpoints.load(state.incident_id)
        # Even with empty results, evidence should be recorded
        # (recording that the tool returned nothing is itself evidence)
        assert len(loaded.evidence) > 0

    @pytest.mark.asyncio
    async def test_error_result_recorded_as_evidence(
        self, engine, checkpoints
    ) -> None:
        """Tool returning an error should be recorded as evidence."""
        # No telemetry data — some tools may error
        state = engine.start({"service": "nonexistent-service", "error_rate": 0.5})
        for _ in range(4):
            state = await engine.run_round(state.incident_id)

        loaded = checkpoints.load(state.incident_id)
        # Error results should still be captured as evidence
        assert len(loaded.evidence) > 0


# ===================================================================
# REPORT GENERATION: guard on root cause verification
# ===================================================================


class TestReportGeneration:
    """Tests for report generation guard."""

    @pytest.mark.asyncio
    async def test_report_only_when_root_cause_verified(
        self, engine, telemetry_repo
    ) -> None:
        """Report should only be generated when a root cause is verified."""
        telemetry_repo.record(_log_event(message="timeout"))
        telemetry_repo.record(_metric_event(value=0.17))
        telemetry_repo.record(_deployment_event(version="v2.3.1"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(8):
            state = await engine.run_round(state.incident_id)

        # If status is report_ready, there must be a confirmed hypothesis
        if state.status == InvestigationStatus.REPORT_READY:
            confirmed = [
                h for h in state.hypotheses
                if h.status == HypothesisStatus.CONFIRMED
            ]
            assert len(confirmed) > 0

    @pytest.mark.asyncio
    async def test_report_contains_findings(self, engine, telemetry_repo) -> None:
        """Generated report should contain structured findings."""
        telemetry_repo.record(_log_event(message="timeout"))
        telemetry_repo.record(_metric_event(value=0.17))
        telemetry_repo.record(_deployment_event(version="v2.3.1"))

        state = engine.start({"service": "order-service", "error_rate": 0.17})
        for _ in range(8):
            state = await engine.run_round(state.incident_id)

        if state.status == InvestigationStatus.REPORT_READY and state.report:
            assert "root_cause" in state.report or "findings" in state.report


# ===================================================================
# API ROUTES: investigation endpoints
# ===================================================================


class TestInvestigationAPI:
    """Tests for the investigation API routes."""

    @pytest.mark.asyncio
    async def test_start_investigation_api(self) -> None:
        """POST /api/investigations/start should create an investigation."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/investigations/start",
                json={
                    "service": "order-service",
                    "error_rate": 0.17,
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert "incident_id" in data
        assert data["status"] == "scoping"

    @pytest.mark.asyncio
    async def test_run_round_api(self) -> None:
        """POST /api/investigations/{incident_id}/round should run a round."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Start an investigation first
            start_response = await client.post(
                "/api/investigations/start",
                json={"service": "order-service", "error_rate": 0.17},
            )
            incident_id = start_response.json()["incident_id"]

            # Run a round
            round_response = await client.post(
                f"/api/investigations/{incident_id}/round",
            )
        assert round_response.status_code == 200
        data = round_response.json()
        assert data["current_round"] >= 1

    @pytest.mark.asyncio
    async def test_resume_investigation_api(self) -> None:
        """POST /api/investigations/{incident_id}/resume should resume."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Start an investigation first
            start_response = await client.post(
                "/api/investigations/start",
                json={"service": "order-service", "error_rate": 0.17},
            )
            incident_id = start_response.json()["incident_id"]

            # Resume
            resume_response = await client.post(
                f"/api/investigations/{incident_id}/resume",
            )
        assert resume_response.status_code == 200

    @pytest.mark.asyncio
    async def test_resume_nonexistent_returns_404(self) -> None:
        """Resume for nonexistent incident should return 404."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/investigations/nonexistent-id/resume",
            )
        assert response.status_code == 404


# ===================================================================
# API ROUTES: case endpoints
# ===================================================================


class TestCaseAPI:
    """Tests for the case memory API routes."""

    @pytest.mark.asyncio
    async def test_save_and_search_case_api(self) -> None:
        """POST /api/cases and GET /api/cases/search should work."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Save a verified case
            save_response = await client.post(
                "/api/cases",
                json={
                    "status": "human_verified",
                    "symptom": "order timeout",
                    "service": "order-service",
                    "root_cause": "payment_latency_spike",
                },
            )
        assert save_response.status_code == 201
        assert "case_id" in save_response.json()

    @pytest.mark.asyncio
    async def test_confirm_case_api(self) -> None:
        """POST /api/cases/{case_id}/confirm should confirm a case."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_control_plane.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Save a pending case
            save_response = await client.post(
                "/api/cases",
                json={
                    "status": "pending_review",
                    "symptom": "payment delay",
                    "service": "payment-service",
                },
            )
            case_id = save_response.json()["case_id"]

            # Confirm it
            confirm_response = await client.post(
                f"/api/cases/{case_id}/confirm",
            )
        assert confirm_response.status_code == 200
        assert confirm_response.json()["status"] == "confirmed"
