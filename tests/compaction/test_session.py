"""Tests for session memory projection and persistence.

Verifies:
- Projection preserves exact evidence IDs
- Missing evidence reference disables fast compaction
- Atomic persistence
"""

import os
from pathlib import Path

import pytest

from incidentlens_contracts.models import Hypothesis, HypothesisStatus
from incidentlens_control_plane.compaction.domain import CompactionConfig
from incidentlens_control_plane.compaction.session import (
    SessionMemorySnapshot,
    SessionMemoryStore,
    SessionMemoryValidation,
    project_session_memory,
    validate_session_memory,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_state() -> dict:
    """Sample investigation state for testing."""
    return {
        "incident_id": "inc-123",
        "status": "investigating",
        "phase": "agent_loop",
        "current_round": 3,
        "max_rounds": 8,
        "alert": {"summary": "High latency in order-service"},
        "hypotheses": [
            {
                "id": "hyp-1",
                "description": "Database connection pool exhaustion",
                "confidence": 0.6,
                "supporting_evidence_ids": ["ev-1", "ev-2"],
                "contradicting_evidence_ids": ["ev-3"],
                "status": "active",
                "root_service": "order-service",
                "cause_code": "db_pool_exhaustion",
            }
        ],
        "evidence": [
            {
                "id": "ev-1",
                "source_tool": "read_metrics",
                "tool_call_id": "tc-1",
                "content": {"summary": "DB connection pool at 95%"},
                "supports_hypothesis_ids": ["hyp-1"],
                "contradicts_hypothesis_ids": [],
            },
            {
                "id": "ev-2",
                "source_tool": "read_logs",
                "tool_call_id": "tc-2",
                "content": {"summary": "Connection timeout errors"},
                "supports_hypothesis_ids": ["hyp-1"],
                "contradicts_hypothesis_ids": [],
            },
            {
                "id": "ev-3",
                "source_tool": "read_logs",
                "tool_call_id": "tc-3",
                "content": {"summary": "CPU spike before latency"},
                "supports_hypothesis_ids": [],
                "contradicts_hypothesis_ids": ["hyp-1"],
            },
        ],
        "loaded_skill_names": ["downstream-timeout", "db-pool-monitor"],
        "model_call_count": 5,
        "tool_call_count": 12,
        "fallback_used": False,
        "last_error_code": None,
    }


@pytest.fixture
def sample_messages() -> list[dict]:
    """Sample message history for testing."""
    return [
        {"role": "system", "content": "You are an incident investigator."},
        {"role": "user", "content": "Investigate high latency in order-service"},
        {"role": "assistant", "content": "I will investigate the latency issue."},
        {"role": "tool", "content": "DB connection pool at 95%"},
        {"role": "tool", "content": "Connection timeout errors"},
    ]


@pytest.fixture
def session_store(tmp_path: Path) -> SessionMemoryStore:
    """Create a temporary session store for testing."""
    return SessionMemoryStore(tmp_path / "sessions")


# ---------------------------------------------------------------------------
# Projection tests
# ---------------------------------------------------------------------------


class TestProjectSessionMemory:
    """Tests for project_session_memory function."""

    def test_preserves_exact_evidence_ids(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Projection preserves exact evidence IDs from state."""
        snapshot = project_session_memory(sample_state, sample_messages)

        assert snapshot.evidence_ids == ["ev-1", "ev-2", "ev-3"]
        assert snapshot.incident_id == "inc-123"

    def test_preserves_hypothesis_evidence_references(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Projection preserves hypothesis evidence references."""
        snapshot = project_session_memory(sample_state, sample_messages)

        assert len(snapshot.hypotheses) == 1
        hyp = snapshot.hypotheses[0]
        assert hyp.supporting_evidence_ids == ["ev-1", "ev-2"]
        assert hyp.contradicting_evidence_ids == ["ev-3"]

    def test_extracts_verified_facts_from_evidence(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Projection extracts verified facts from evidence content."""
        snapshot = project_session_memory(sample_state, sample_messages)

        assert "DB connection pool at 95%" in snapshot.verified_facts
        assert "Connection timeout errors" in snapshot.verified_facts
        assert "CPU spike before latency" in snapshot.verified_facts

    def test_preserves_loaded_skills(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Projection preserves loaded skill names."""
        snapshot = project_session_memory(sample_state, sample_messages)

        assert "downstream-timeout" in snapshot.loaded_skills
        assert "db-pool-monitor" in snapshot.loaded_skills

    def test_calculates_budget_from_messages(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Projection calculates budget from message history."""
        snapshot = project_session_memory(sample_state, sample_messages)

        assert snapshot.budget.current_messages == 5
        assert snapshot.budget.current_tokens > 0

    def test_determines_next_action_from_status(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Projection determines next action from status."""
        # Default status
        snapshot = project_session_memory(sample_state, sample_messages)
        assert snapshot.next_action == "Continue investigation"

        # Needs more evidence
        sample_state["status"] = "needs_more_evidence"
        snapshot = project_session_memory(sample_state, sample_messages)
        assert snapshot.next_action == "Gather more evidence"

        # Report ready
        sample_state["status"] = "report_ready"
        snapshot = project_session_memory(sample_state, sample_messages)
        assert snapshot.next_action == "Generate report"

    def test_empty_state_produces_valid_snapshot(self) -> None:
        """Projection handles empty state gracefully."""
        state = {"incident_id": "inc-empty"}
        messages = []

        snapshot = project_session_memory(state, messages)

        assert snapshot.incident_id == "inc-empty"
        assert snapshot.evidence_ids == []
        assert snapshot.hypotheses == []
        assert snapshot.verified_facts == []

    def test_custom_session_id(self, sample_state: dict, sample_messages: list[dict]) -> None:
        """Projection uses custom session ID when provided."""
        snapshot = project_session_memory(
            sample_state, sample_messages, session_id="custom-session"
        )

        assert snapshot.session_id == "custom-session"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateSessionMemory:
    """Tests for validate_session_memory function."""

    def test_validates_with_all_evidence_present(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Validation passes when all evidence IDs are present."""
        snapshot = project_session_memory(sample_state, sample_messages)
        evidence_ids = ["ev-1", "ev-2", "ev-3"]

        validation = validate_session_memory(snapshot, evidence_ids)

        assert validation.valid is True
        assert validation.missing_evidence_ids == []
        assert validation.errors == []

    def test_missing_evidence_reference_detected(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Missing evidence ID is detected and reported."""
        snapshot = project_session_memory(sample_state, sample_messages)
        # Only provide ev-1 and ev-2, missing ev-3
        evidence_ids = ["ev-1", "ev-2"]

        validation = validate_session_memory(snapshot, evidence_ids)

        assert validation.valid is False
        assert "ev-3" in validation.missing_evidence_ids
        assert any("ev-3" in err for err in validation.errors)

    def test_hypothesis_evidence_validation(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Hypothesis evidence references are validated."""
        snapshot = project_session_memory(sample_state, sample_messages)
        # Missing evidence referenced by hypothesis
        evidence_ids = ["ev-1"]

        validation = validate_session_memory(snapshot, evidence_ids)

        assert validation.valid is False
        assert "ev-2" in validation.missing_evidence_ids
        assert "ev-3" in validation.missing_evidence_ids

    def test_no_evidence_list_skips_validation(
        self, sample_state: dict, sample_messages: list[dict]
    ) -> None:
        """Validation skips evidence check when no list provided."""
        snapshot = project_session_memory(sample_state, sample_messages)

        # No evidence_ids provided
        validation = validate_session_memory(snapshot, None)

        assert validation.valid is True
        assert validation.missing_evidence_ids == []

    def test_empty_snapshot_validates(self) -> None:
        """Empty snapshot validates successfully."""
        snapshot = SessionMemorySnapshot(
            incident_id="inc-empty",
            session_id="session-empty",
        )

        validation = validate_session_memory(snapshot, [])

        assert validation.valid is True

    def test_budget_exceeded_detected(self) -> None:
        """Budget exceeded is detected."""
        snapshot = SessionMemorySnapshot(
            incident_id="inc-budget",
            session_id="session-budget",
            budget={
                "max_messages": 100,
                "max_tokens": 100000,
                "current_messages": 150,
                "current_tokens": 0,
            },
        )

        validation = validate_session_memory(snapshot, [])

        assert validation.valid is False
        assert any("exceeds" in err for err in validation.errors)

    def test_warnings_for_empty_fields(self) -> None:
        """Warnings generated for empty objective and next action."""
        snapshot = SessionMemorySnapshot(
            incident_id="inc-warnings",
            session_id="session-warnings",
            objective="",
            next_action="",
        )

        validation = validate_session_memory(snapshot, [])

        assert any("objective" in w.lower() for w in validation.warnings)
        assert any("next action" in w.lower() for w in validation.warnings)


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


class TestSessionMemoryStore:
    """Tests for SessionMemoryStore persistence."""

    def test_atomic_save_and_load(self, session_store: SessionMemoryStore) -> None:
        """Save and load round-trips correctly."""
        snapshot = SessionMemorySnapshot(
            incident_id="inc-atomic",
            session_id="session-atomic",
            objective="Test objective",
            evidence_ids=["ev-1", "ev-2"],
        )

        path = session_store.save(snapshot)
        assert path.exists()

        loaded = session_store.load("inc-atomic", "session-atomic")
        assert loaded is not None
        assert loaded.incident_id == "inc-atomic"
        assert loaded.session_id == "session-atomic"
        assert loaded.objective == "Test objective"
        assert loaded.evidence_ids == ["ev-1", "ev-2"]

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        """Save creates parent directories if needed."""
        store = SessionMemoryStore(tmp_path / "deep" / "nested" / "sessions")
        snapshot = SessionMemorySnapshot(
            incident_id="inc-nested",
            session_id="session-nested",
        )

        path = store.save(snapshot)
        assert path.exists()

    def test_load_nonexistent_returns_none(self, session_store: SessionMemoryStore) -> None:
        """Load returns None for nonexistent session."""
        loaded = session_store.load("inc-nonexistent", "session-nonexistent")
        assert loaded is None

    def test_list_sessions(self, session_store: SessionMemoryStore) -> None:
        """List sessions returns all session IDs for an incident."""
        # Save multiple sessions
        for i in range(3):
            snapshot = SessionMemorySnapshot(
                incident_id="inc-list",
                session_id=f"session-{i}",
            )
            session_store.save(snapshot)

        sessions = session_store.list_sessions("inc-list")
        assert len(sessions) == 3
        assert "session-0" in sessions
        assert "session-1" in sessions
        assert "session-2" in sessions

    def test_delete_session(self, session_store: SessionMemoryStore) -> None:
        """Delete removes a session."""
        snapshot = SessionMemorySnapshot(
            incident_id="inc-delete",
            session_id="session-delete",
        )
        session_store.save(snapshot)

        assert session_store.delete("inc-delete", "session-delete") is True
        assert session_store.load("inc-delete", "session-delete") is None

    def test_delete_nonexistent_returns_false(self, session_store: SessionMemoryStore) -> None:
        """Delete returns False for nonexistent session."""
        assert session_store.delete("inc-nonexistent", "session-nonexistent") is False

    def test_atomic_write_on_crash(self, session_store: SessionMemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """Atomic write leaves no temp files on crash."""
        snapshot = SessionMemorySnapshot(
            incident_id="inc-crash",
            session_id="session-crash",
        )

        # Patch os.replace to raise an exception
        original_replace = os.replace

        def failing_replace(src: str, dst: str | Path) -> None:
            if "session-crash" in str(dst):
                raise OSError("Simulated crash")
            return original_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_replace)

        # Save should fail
        with pytest.raises(RuntimeError, match="Failed to persist"):
            session_store.save(snapshot)

        # Check no temp files remain
        incident_dir = session_store._session_dir("inc-crash")
        if incident_dir.exists():
            temp_files = list(incident_dir.glob("*.tmp"))
            assert len(temp_files) == 0


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------


class TestCompactionConfig:
    """Tests for CompactionConfig domain type."""

    def test_default_config(self) -> None:
        """Default config has sensible limits."""
        config = CompactionConfig()

        assert config.max_messages_before_compact == 200
        assert config.max_messages_after_compact == 50
        assert config.min_messages_to_compact == 100
        assert config.require_all_evidence_ids is True

    def test_frozen_config(self) -> None:
        """Config is immutable."""
        config = CompactionConfig()

        with pytest.raises(Exception):
            config.max_messages_before_compact = 100  # type: ignore[misc]
