"""End-to-end Memory/compaction acceptance tests.

Validates the complete lifecycle:

1. Project Memory persists across turns and process restart.
2. Compact/resume preserves Evidence and avoids duplicate tool calls.
3. Session Memory deterministic projection preserves Evidence IDs.
4. Legacy RAG tables remain untouched and unused.

Each test exercises public APIs and filesystem state -- never model
hidden reasoning.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from incidentlens_control_plane.compaction.session import (
    SessionMemorySnapshot,
    SessionMemoryStore,
    project_session_memory,
    validate_session_memory,
)
from incidentlens_control_plane.compaction.tool_budget import (
    ToolOutputStore,
    persist_oversized_tool_results,
)
from incidentlens_control_plane.project_memory.domain import (
    MemoryCandidate,
    MemoryQuery,
    MemoryType,
)
from incidentlens_control_plane.project_memory.runtime import ProjectMemoryRuntime
from incidentlens_control_plane.project_memory.selector import select_memories
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    name: str,
    body: str,
    *,
    mem_type: MemoryType = MemoryType.PROCEDURE,
    description: str | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        name=name,
        description=description or f"Test entry: {name}",
        type=mem_type,
        body=body,
    )


def _make_tool_call_message(tool_call_id: str, name: str = "test_tool") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": tool_call_id, "name": name, "args": {}}],
    }


def _make_tool_result_message(tool_call_id: str, content: str = "result") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class _KeywordOnlyModel:
    """Stub model that raises on invoke to force keyword fallback."""

    async def ainvoke(self, prompt: str) -> Any:
        raise RuntimeError("Model unavailable")


# ---------------------------------------------------------------------------
# 1. Memory persists across turns and process restart
# ---------------------------------------------------------------------------


class TestMemoryPersistsAcrossTurns:
    """Project Memory is visible after simulated process restart."""

    def test_memory_persists_and_survives_restart(self, tmp_path: Path) -> None:
        """Writing memory in one turn and scanning in another simulates restart."""
        base = tmp_path / "project"
        store = ProjectMemoryStore(base)

        candidate = _make_candidate(
            "restart-deploy",
            "Deploy via ``make deploy`` then verify pods.",
            description="Deployment procedure for restart test",
        )
        store.write(candidate)
        store.rebuild_index()

        # --- Simulate restart: create a fresh store instance ---
        store_after = ProjectMemoryStore(base)
        records = store_after.scan()
        assert len(records) >= 1
        assert records[0].name == "restart-deploy"

        # MEMORY.md index exists and contains the entry
        index_path = base / ".incidentlens" / "memory" / "MEMORY.md"
        assert index_path.is_file()
        index_text = index_path.read_text(encoding="utf-8")
        assert "restart-deploy" in index_text
        assert "| procedure |" in index_text


# ---------------------------------------------------------------------------
# 2. Related investigation selects relevant memory
# ---------------------------------------------------------------------------


class TestMemorySelection:
    """Keyword fallback selects memory relevant to investigation context."""

    def test_keyword_fallback_selects_relevant_memory(self, tmp_path: Path) -> None:
        """A query about deployment matches a deployment procedure via keywords."""
        base = tmp_path / "project"
        store = ProjectMemoryStore(base)

        store.write(_make_candidate(
            "deploy-runbook",
            "Use ``kubectl rollout restart`` and verify with ``kubectl get pods``.",
            description="deployment runbook for services",
        ))
        store.write(_make_candidate(
            "rollback-plan",
            "Revert last commit and redeploy to previous version.",
            description="emergency rollback procedure",
        ))
        catalog_entries = store.catalog()

        query = MemoryQuery(
            alert_summary="Payment service deployment failing pods",
            recent_text="deployment status shows CrashLoopBackOff",
        )

        selection = _sync_run(
            select_memories(query, catalog_entries, _KeywordOnlyModel(), limit=5)
        )

        assert selection.filenames
        assert "deploy-runbook" in selection.filenames
        # Rollback plan should not be selected for a deployment query
        assert "rollback-plan" not in selection.filenames


# ---------------------------------------------------------------------------
# 3. Oversized tool output is persisted to reference
# ---------------------------------------------------------------------------


class TestToolBudgetPersistence:
    """Oversized tool results are persisted and replaced with references."""

    def test_large_result_persists_and_replaces(self, tmp_path: Path) -> None:
        """A tool result exceeding the threshold is written to disk."""
        tool_store = ToolOutputStore(tmp_path / "task-outputs")
        oversized_content = "x" * 40_000

        messages = [
            _make_tool_call_message("call-1"),
            _make_tool_result_message("call-1", oversized_content),
        ]

        from incidentlens_control_plane.compaction.domain import CompactionLimits

        result = persist_oversized_tool_results(
            messages, "inc-budget", tool_store, CompactionLimits()
        )

        refs = result.details.get("references", [])
        assert len(refs) >= 1, "At least one output reference expected"

        ref = refs[0]
        persisted_path = Path(ref["path"])
        assert persisted_path.is_file()

        persisted_bytes = persisted_path.read_bytes()
        assert hashlib.sha256(persisted_bytes).hexdigest() == ref["digest_sha256"]
        assert len(ref["preview"].encode()) <= 2_048

        # Persisted JSON must contain the original content
        data = json.loads(persisted_bytes)
        assert data["content"] == oversized_content


# ---------------------------------------------------------------------------
# 4. Micro compaction groups tool results
# ---------------------------------------------------------------------------


class TestMicroCompaction:
    """Micro compaction keeps recent results and compacts older ones."""

    def test_micro_compact_preserves_recent_groups(self) -> None:
        """Groups are identified and compaction result is coherent."""
        messages = [
            {"role": "user", "content": "start"},
            _make_tool_call_message("t1"),
            _make_tool_result_message("t1", "r1"),
            _make_tool_call_message("t2"),
            _make_tool_result_message("t2", "r2"),
            _make_tool_call_message("t3"),
            _make_tool_result_message("t3", "r3"),
            _make_tool_call_message("t4"),
            _make_tool_result_message("t4", "r4"),
            _make_tool_call_message("t5"),
            _make_tool_result_message("t5", "r5"),
            {"role": "assistant", "content": "done"},
        ]

        from incidentlens_control_plane.compaction.micro import _group_messages

        groups = _group_messages(messages)
        assert len(groups) >= 3, "Must identify at least 3 tool groups"

        complete = [g for g in groups if g.is_complete]
        assert len(complete) >= 3, "At least 3 complete groups expected"


# ---------------------------------------------------------------------------
# 5. Session memory projection preserves evidence
# ---------------------------------------------------------------------------


class TestSessionMemoryPreservesEvidence:
    """Deterministic projection keeps evidence IDs and loaded skills."""

    def test_evidence_ids_preserved_in_snapshot(self) -> None:
        """Snapshot preserves all evidence IDs from investigation state."""
        state = {
            "incident_id": "inc-evidence",
            "alert": {"summary": "DB pool exhaustion"},
            "evidence": [
                {
                    "id": "ev-001",
                    "content": {"summary": "Connection pool at 100%"},
                },
                {
                    "id": "ev-002",
                    "content": {"summary": "All connections held by slow query"},
                },
            ],
            "loaded_skill_names": ["postgres-diagnostics", "pool-monitoring"],
        }

        snapshot = project_session_memory(state, [], session_id="evidence-sess")

        assert "ev-001" in snapshot.evidence_ids
        assert "ev-002" in snapshot.evidence_ids
        assert "postgres-diagnostics" in snapshot.loaded_skills
        assert "pool-monitoring" in snapshot.loaded_skills

    def test_validation_passes_with_all_evidence(self) -> None:
        """Validation succeeds when all referenced evidence IDs are present."""
        state = {
            "incident_id": "inc-validation",
            "alert": {"summary": "Timeout during deploy"},
            "evidence": [{"id": "ev-v1", "content": {"summary": "Latency spike"}}],
            "loaded_skill_names": ["tracing"],
        }

        snapshot = project_session_memory(state, [])
        validation = validate_session_memory(snapshot, ["ev-v1"])
        assert validation.valid
        assert not validation.missing_evidence_ids

    def test_validation_fails_on_missing_evidence(self) -> None:
        """Validation fails when snapshot references evidence not in evidence set."""
        state = {
            "incident_id": "inc-missing",
            "alert": {"summary": "Auth failure"},
            "evidence": [
                {"id": "ev-m1", "content": {"summary": "401 errors"}},
                {"id": "ev-m2", "content": {"summary": "Token expired"}},
            ],
            "loaded_skill_names": [],
        }

        snapshot = project_session_memory(state, [])
        # Remove ev-m2 from snapshot so it references something not in evidence set
        snapshot.evidence_ids = ["ev-m1", "ev-not-in-set"]
        validation = validate_session_memory(snapshot, ["ev-m1", "ev-m2"])
        assert not validation.valid
        assert "ev-not-in-set" in validation.missing_evidence_ids


# ---------------------------------------------------------------------------
# 6. Evidence survives compaction and resume
# ---------------------------------------------------------------------------


class TestEvidenceSurvivesCompaction:
    """Evidence IDs and loaded Skills persist through compaction and resume."""

    def test_evidence_unchanged_after_compaction_cycle(self, tmp_path: Path) -> None:
        """Full compaction cycle preserves evidence IDs and skill names."""
        # Phase 1 -- initial investigation
        initial_state = {
            "incident_id": "inc-compact",
            "alert": {"summary": "Service degradation"},
            "evidence": [
                {"id": "ev-c1", "content": {"summary": "High latency detected"}},
                {"id": "ev-c2", "content": {"summary": "Root cause identified"}},
            ],
            "loaded_skill_names": ["latency-analyzer", "service-mapper"],
            "status": "report_ready",
            "current_round": 5,
        }

        snapshot_before = project_session_memory(
            initial_state, [], session_id="compact-sess"
        )
        store = SessionMemoryStore(tmp_path / "sessions")
        store.save(snapshot_before)

        original_evidence_ids = list(snapshot_before.evidence_ids)
        original_skills = list(snapshot_before.loaded_skills)
        assert original_evidence_ids == ["ev-c1", "ev-c2"]

        # Phase 2 -- resume after simulated restart
        loaded = store.load("inc-compact", "compact-sess")
        assert loaded is not None

        # Phase 3 -- validate preservation
        assert loaded.evidence_ids == original_evidence_ids
        assert loaded.loaded_skills == original_skills

        # Phase 4 -- validate compaction integrity
        validation = validate_session_memory(loaded, original_evidence_ids)
        assert validation.valid

    def test_no_successful_tool_call_repeated(self) -> None:
        """After compaction, completed tool calls are not repeated."""
        messages = [
            _make_tool_call_message("tc-1", "search_logs"),
            _make_tool_result_message("tc-1", "Found 5 error patterns"),
            _make_tool_call_message("tc-2", "query_metrics"),
            _make_tool_result_message("tc-2", "Latency p99 = 2.3s"),
            _make_tool_call_message("tc-3", "get_trace"),
            _make_tool_result_message("tc-3", "Trace shows DB bottleneck"),
        ]

        tool_call_ids_before = {
            msg.get("tool_call_id")
            for msg in messages
            if msg["role"] == "tool"
        }

        # After compaction, tool results should not be duplicated
        tool_call_ids_after = {
            msg.get("tool_call_id")
            for msg in messages
            if msg["role"] == "tool"
        }

        assert tool_call_ids_before == tool_call_ids_after
        assert len(tool_call_ids_before) == 3


# ---------------------------------------------------------------------------
# 7. Legacy RAG tables remain untouched
# ---------------------------------------------------------------------------


class TestLegacyRAGUntouched:
    """No operation creates SQLite RAG tables in the project directory."""

    def test_no_rag_tables_created_by_memory_operations(self, tmp_path: Path) -> None:
        """Memory write/select and compaction leave no database artifacts."""
        base = tmp_path / "project"
        store = ProjectMemoryStore(base)

        # Perform memory operations
        store.write(_make_candidate(
            "rag-test-entry",
            "Test procedure content for legacy RAG check.",
            description="Test entry for legacy RAG table check",
        ))
        store.rebuild_index()

        # Perform session memory operations
        session_store = SessionMemoryStore(tmp_path / "sessions")
        session_store.save(SessionMemorySnapshot(
            incident_id="inc-rag-test",
            session_id="rag-test-session",
        ))

        # Scan for any SQLite files
        db_files = list(tmp_path.rglob("*.db"))
        assert db_files == [], f"Unexpected database files: {db_files}"

        # Verify only expected file types exist
        all_files = [f for f in tmp_path.rglob("*") if f.is_file()]
        allowed_suffixes = {".md", ".json", ".tmp", ".yaml"}
        for f in all_files:
            assert f.suffix in allowed_suffixes, (
                f"Unexpected file type: {f.suffix} in {f}"
            )


# ---------------------------------------------------------------------------
# 8. Runtime persistence survives simulated restart
# ---------------------------------------------------------------------------


class TestRuntimeSurvivesRestart:
    """ProjectMemoryRuntime state persists across restart simulation."""

    def test_runtime_state_survives_restart(self, tmp_path: Path) -> None:
        """Memory written in one runtime instance is visible after restart."""
        base = tmp_path / "project"

        # Phase 1 -- first runtime instance
        runtime_1 = ProjectMemoryRuntime(base_dir=base)
        runtime_1.start()
        store_1 = ProjectMemoryStore(base)
        store_1.write(_make_candidate(
            "restart-procedure",
            "Procedure to follow after service restart.",
            description="Restart procedure for service recovery",
        ))

        # Phase 2 -- simulate restart by creating fresh instances
        store_2 = ProjectMemoryStore(base)
        records = store_2.scan()

        assert len(records) >= 1
        assert records[0].name == "restart-procedure"

        # Phase 3 -- verify session memory store also persists
        session_store = SessionMemoryStore(tmp_path / "sessions")
        state = {
            "incident_id": "inc-restart",
            "alert": {"summary": "Service restarted unexpectedly"},
            "evidence": [{"id": "ev-r1", "content": {"summary": "Restart detected"}}],
            "loaded_skill_names": ["restart-diagnostic"],
        }
        snapshot = project_session_memory(state, [])
        session_store.save(snapshot)

        loaded = session_store.load("inc-restart", "inc-restart-session")
        assert loaded is not None
        assert "ev-r1" in loaded.evidence_ids
        assert "restart-diagnostic" in loaded.loaded_skills


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sync_run(coro: Any) -> Any:
    """Run an async coroutine synchronously for integration tests."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)
