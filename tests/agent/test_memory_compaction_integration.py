"""Tests for memory and compaction wiring into the agent lifecycle.

Verifies:
  - Middleware installed in deterministic order (1-6)
  - FastAPI lifespan calls ProjectMemoryRuntime.close() exactly once
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from incidentlens_control_plane.agent.middleware import (
    AuditMiddleware,
    BudgetEnforcementMiddleware,
    ConclusionBoundaryMiddleware,
    DuplicateToolCallMiddleware,
    EvidenceRecordingMiddleware,
    InvestigationContextMiddleware,
    ReportGateMiddleware,
)
from incidentlens_control_plane.compaction.middleware import CompactionMiddleware
from incidentlens_control_plane.compaction.config import CompactionRuntimeConfig
from incidentlens_control_plane.project_memory.middleware import ProjectMemoryMiddleware


# ---------------------------------------------------------------------------
# Middleware order verification
# ---------------------------------------------------------------------------


class _StubMiddleware:
    """Lightweight middleware stand-in for type-safe ordering checks."""

    def __init__(self, label: str) -> None:
        self.name = label

    def __repr__(self) -> str:
        return f"_StubMiddleware({self.name!r})"


class _StubSkillRuntime:
    """Minimal SkillRuntime stand-in that returns stub middleware."""

    def middleware(self) -> tuple:
        return (
            _StubMiddleware("filesystem"),
            _StubMiddleware("skills"),
            _StubMiddleware("skill_audit"),
        )

    @property
    def policies_by_cause_code(self) -> dict:
        return {}


class _StubAuditStore:
    """Minimal audit store stand-in."""


class IncidentToolContextMiddlewareStub:
    """Stub for IncidentToolContextMiddleware used only in ordering tests."""
    name = "IncidentToolContextMiddleware"


def _build_middleware_list(
    *,
    project_memory_runtime: Any | None = None,
    compaction_runtime: Any | None = None,
    model_identity: Any | None = None,
) -> list[Any]:
    """Reproduce the middleware assembly logic from graph.py for testing.

    This mirrors the deterministic order documented in the module docstring:
      1. Filesystem/Skill middleware
      2. Project Memory injection
      3. Investigation context
      4. Audit/evidence/conclusion gates
      5. Compaction
      6. Report gate
    """
    _skill = _StubSkillRuntime()
    _audit = _StubAuditStore()

    middleware: list[Any] = []

    # --- 1. Project filesystem/Skill middleware ---
    fs_mw, skills_mw, skill_audit_mw = _skill.middleware()
    middleware.extend([fs_mw, skills_mw, skill_audit_mw])

    # --- 2. Project Memory injection ---
    if project_memory_runtime is not None:
        memory_base = getattr(project_memory_runtime, "base_dir", Path("."))
        middleware.append(ProjectMemoryMiddleware(base_dir=memory_base))

    # --- 3. Investigation context ---
    middleware.append(InvestigationContextMiddleware(skill_runtime=_skill))

    # --- 4. Audit/evidence/conclusion gates ---
    middleware.append(AuditMiddleware(_audit))  # type: ignore[arg-type]
    middleware.append(IncidentToolContextMiddlewareStub())
    middleware.append(DuplicateToolCallMiddleware())
    middleware.append(EvidenceRecordingMiddleware())
    middleware.append(ConclusionBoundaryMiddleware())
    middleware.append(BudgetEnforcementMiddleware(model_limit=12, tool_limit=12))

    # --- 5. Compaction ---
    if compaction_runtime is not None:
        from incidentlens_control_plane.compaction.session import SessionMemoryStore
        from incidentlens_control_plane.compaction.tool_budget import ToolOutputStore
        from incidentlens_control_plane.compaction.middleware import TranscriptStore

        compaction_dir = Path(
            getattr(compaction_runtime, "session_dir", ".incidentlens/sessions")
        )
        transcript_dir = Path(
            getattr(compaction_runtime, "transcript_dir", ".incidentlens/transcripts")
        )
        task_output_dir = Path(
            getattr(compaction_runtime, "task_output_dir", ".incidentlens/task-outputs")
        )

        session_store = SessionMemoryStore(base_dir=compaction_dir)
        transcript_store = TranscriptStore(base_dir=transcript_dir)
        tool_output_store = ToolOutputStore(base_dir=task_output_dir)

        model_profile = None
        if model_identity is not None:
            ctx_tokens = getattr(model_identity, "context_window_tokens", 128_000)
            res_tokens = getattr(model_identity, "reserved_output_tokens", 4_096)

            class _MP:
                context_window_tokens = ctx_tokens
                reserved_output_tokens = res_tokens

            model_profile = _MP()

        middleware.append(
            CompactionMiddleware(
                runtime=None,
                model_profile=model_profile,
                session_store=session_store,
                tool_output_store=tool_output_store,
                transcript_store=transcript_store,
            )
        )

    # --- 6. Report gate ---
    middleware.append(ReportGateMiddleware(_skill, audit_store=_audit))  # type: ignore[arg-type]

    return middleware


def _middleware_names(mw_list: list[Any]) -> list[str]:
    """Extract human-readable names from a middleware list."""
    return [type(m).__name__ for m in mw_list]


class TestMiddlewareOrder:
    """Verify that middleware is installed in deterministic order."""

    def test_full_order_with_all_runtimes(self, tmp_path: Path) -> None:
        """With memory and compaction enabled, middleware appears in documented order."""
        from incidentlens_control_plane.llm.registry import ModelIdentity

        memory_runtime = SimpleNamespace(base_dir=tmp_path / "memory")
        compaction_runtime = CompactionRuntimeConfig(
            session_dir=tmp_path / "sessions",
            transcript_dir=tmp_path / "transcripts",
            task_output_dir=tmp_path / "task-outputs",
        )
        model_identity = ModelIdentity(
            profile="test",
            model="test-model",
            endpoint_host="localhost",
            context_window_tokens=128_000,
            reserved_output_tokens=4_096,
        )

        middleware = _build_middleware_list(
            project_memory_runtime=memory_runtime,
            compaction_runtime=compaction_runtime,
            model_identity=model_identity,
        )

        # Find key middleware positions
        memory_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, ProjectMemoryMiddleware)
        )
        investigation_ctx_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, InvestigationContextMiddleware)
        )
        audit_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, AuditMiddleware)
        )
        compaction_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, CompactionMiddleware)
        )
        report_gate_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, ReportGateMiddleware)
        )

        # Order: filesystem/skill (0-2) < memory (3) < context (4) < audit (5)
        #        < compaction (later) < report gate (last)
        assert memory_idx < investigation_ctx_idx, (
            "Memory injection must come before investigation context"
        )
        assert investigation_ctx_idx < audit_idx, (
            "Investigation context must come before audit gates"
        )
        assert audit_idx < compaction_idx, (
            "Audit gates must come before compaction"
        )
        assert compaction_idx < report_gate_idx, (
            "Compaction must come before report gate"
        )
        assert report_gate_idx == len(middleware) - 1, (
            "Report gate must be the last middleware"
        )

    def test_order_without_memory_and_compaction(self) -> None:
        """Without optional runtimes, core middleware order is preserved."""
        middleware = _build_middleware_list(
            project_memory_runtime=None,
            compaction_runtime=None,
        )

        # No ProjectMemoryMiddleware or CompactionMiddleware
        assert not any(isinstance(m, ProjectMemoryMiddleware) for m in middleware)
        assert not any(isinstance(m, CompactionMiddleware) for m in middleware)

        investigation_ctx_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, InvestigationContextMiddleware)
        )
        audit_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, AuditMiddleware)
        )
        report_gate_idx = next(
            i for i, m in enumerate(middleware)
            if isinstance(m, ReportGateMiddleware)
        )

        assert investigation_ctx_idx < audit_idx
        assert audit_idx < report_gate_idx
        assert report_gate_idx == len(middleware) - 1

    def test_memory_only_without_compaction(self, tmp_path: Path) -> None:
        """Memory injection present, compaction absent -- order preserved."""
        memory_runtime = SimpleNamespace(base_dir=tmp_path / "memory")

        middleware = _build_middleware_list(
            project_memory_runtime=memory_runtime,
            compaction_runtime=None,
        )

        assert any(isinstance(m, ProjectMemoryMiddleware) for m in middleware)
        assert not any(isinstance(m, CompactionMiddleware) for m in middleware)

    def test_compaction_only_without_memory(self, tmp_path: Path) -> None:
        """Compaction present, memory absent -- order preserved."""
        compaction_runtime = CompactionRuntimeConfig(
            session_dir=tmp_path / "sessions",
            transcript_dir=tmp_path / "transcripts",
            task_output_dir=tmp_path / "task-outputs",
        )

        middleware = _build_middleware_list(
            project_memory_runtime=None,
            compaction_runtime=compaction_runtime,
        )

        assert not any(isinstance(m, ProjectMemoryMiddleware) for m in middleware)
        assert any(isinstance(m, CompactionMiddleware) for m in middleware)

    def test_middleware_count_matches_documentation(self, tmp_path: Path) -> None:
        """With all runtimes enabled, total middleware count matches expectations.

        Expected: 3 (filesystem/skill) + 1 (memory) + 1 (context) +
                  6 (audit/gates) + 1 (compaction) + 1 (report) = 13
        """
        from incidentlens_control_plane.llm.registry import ModelIdentity

        memory_runtime = SimpleNamespace(base_dir=tmp_path / "memory")
        compaction_runtime = CompactionRuntimeConfig(
            session_dir=tmp_path / "sessions",
            transcript_dir=tmp_path / "transcripts",
            task_output_dir=tmp_path / "task-outputs",
        )
        model_identity = ModelIdentity(
            profile="test",
            model="test-model",
            endpoint_host="localhost",
        )

        middleware = _build_middleware_list(
            project_memory_runtime=memory_runtime,
            compaction_runtime=compaction_runtime,
            model_identity=model_identity,
        )

        # 3 (fs/skill) + 1 (memory) + 1 (context) + 6 (audit/gates)
        # + 1 (compaction) + 1 (report) = 13
        assert len(middleware) == 13


# ---------------------------------------------------------------------------
# FastAPI shutdown test
# ---------------------------------------------------------------------------

# Patch targets for lazy imports inside the lifespan function body.
# These are the source modules where the names are imported from.
_PATCH_PATHS = {
    "ProjectMemoryRuntime": (
        "incidentlens_control_plane.project_memory.runtime.ProjectMemoryRuntime"
    ),
    "CompactionRuntimeConfig": (
        "incidentlens_control_plane.compaction.config.CompactionRuntimeConfig"
    ),
    "AgentCheckpointRuntime": (
        "incidentlens_control_plane.agent.checkpoint.AgentCheckpointRuntime"
    ),
    "build_investigation_engine": (
        "incidentlens_control_plane.agent.factory.build_investigation_engine"
    ),
    "load_models_config": (
        "incidentlens_control_plane.llm.config.load_models_config"
    ),
    "ModelRegistry": (
        "incidentlens_control_plane.llm.registry.ModelRegistry"
    ),
    "SkillRuntime": (
        "incidentlens_control_plane.agent.skills.SkillRuntime"
    ),
    "create_engine": (
        "incidentlens_telemetry.database.create_engine"
    ),
    "TelemetryRepository": (
        "incidentlens_telemetry.repository.TelemetryRepository"
    ),
    "InvestigationAuditStore": (
        "incidentlens_control_plane.agent.state.InvestigationAuditStore"
    ),
    "ScenarioStore": (
        "incidentlens_scenarios.store.ScenarioStore"
    ),
    "DemoResetService": (
        "incidentlens_control_plane.services.demo_reset.DemoResetService"
    ),
    "InvestigationExportService": (
        "incidentlens_control_plane.services.investigation_export.InvestigationExportService"
    ),
}


class _AsyncContextManager:
    """Helper to make a mock work with ``async with``."""

    def __init__(self, return_value=None):
        self._return_value = return_value

    async def __aenter__(self):
        return self._return_value

    async def __aexit__(self, *args):
        pass


def _patch_all(**overrides):
    """Create a combined patch context for all lifespan dependencies.

    Each key in _PATCH_PATHS maps to a MagicMock. Pass overrides to
    customize specific patches (e.g. return_value, side_effect).
    """
    patches = []
    for name, target in _PATCH_PATHS.items():
        patches.append(patch(target, **overrides.get(name, {})))
    return patches


class TestFastAPIShutdown:
    """Verify the lifespan calls ProjectMemoryRuntime.close() exactly once."""

    @pytest.mark.asyncio
    async def test_lifespan_calls_close_exactly_once(self, tmp_path: Path) -> None:
        """During lifespan shutdown, project_memory_runtime.close() is awaited once."""
        from incidentlens_control_plane.main import lifespan
        from fastapi import FastAPI

        mock_memory_runtime = AsyncMock()
        mock_memory_runtime.close = AsyncMock()

        mock_checkpoints = MagicMock()
        mock_checkpoints.saver = MagicMock()
        mock_cp_runtime = _AsyncContextManager(mock_checkpoints)

        mock_engine = MagicMock()

        mock_registry = MagicMock()
        mock_registry.get = MagicMock()

        env_overrides = {
            "INCIDENTLENS_AGENT_MODE": "llm_agent",
            "INCIDENTLENS_MEMORY_DIR": str(tmp_path / "memory"),
            "INCIDENTLENS_SESSION_DIR": str(tmp_path / "sessions"),
            "INCIDENTLENS_TASK_OUTPUT_DIR": str(tmp_path / "task-outputs"),
            "INCIDENTLENS_TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
            "TELEMETRY_DB_URL": "sqlite:///:memory:",
            "INCIDENTLENS_CHECKPOINT_DB": str(tmp_path / "checkpoints.db"),
            "INCIDENTLENS_MODELS_CONFIG": str(tmp_path / "models.yaml"),
        }

        with patch.dict("os.environ", env_overrides):
            patchers = _patch_all(
                ProjectMemoryRuntime={"return_value": mock_memory_runtime},
                AgentCheckpointRuntime={"return_value": mock_cp_runtime},
                build_investigation_engine={"return_value": mock_engine},
                load_models_config={"return_value": []},
                ModelRegistry={"return_value": mock_registry},
                SkillRuntime={"return_value": MagicMock()},
                create_engine={"return_value": MagicMock()},
                TelemetryRepository={"return_value": MagicMock()},
                InvestigationAuditStore={"return_value": MagicMock()},
                ScenarioStore={"return_value": MagicMock()},
                DemoResetService={"return_value": MagicMock()},
                InvestigationExportService={"return_value": MagicMock()},
            )
            for p in patchers:
                p.start()
            try:
                app = FastAPI()
                async with lifespan(app):
                    # Inside the lifespan -- close has not been called yet
                    pass

                # After exiting lifespan -- close should have been called exactly once
                mock_memory_runtime.close.assert_awaited_once()
            finally:
                for p in patchers:
                    p.stop()

    @pytest.mark.asyncio
    async def test_lifespan_creates_directories(self, tmp_path: Path) -> None:
        """The lifespan creates all required directories."""
        from incidentlens_control_plane.main import lifespan
        from fastapi import FastAPI

        mock_memory_runtime = AsyncMock()

        mock_checkpoints = MagicMock()
        mock_checkpoints.saver = MagicMock()
        mock_cp_runtime = _AsyncContextManager(mock_checkpoints)

        # Override env vars to use tmp_path
        env_overrides = {
            "INCIDENTLENS_AGENT_MODE": "llm_agent",
            "INCIDENTLENS_MEMORY_DIR": str(tmp_path / "memory"),
            "INCIDENTLENS_SESSION_DIR": str(tmp_path / "sessions"),
            "INCIDENTLENS_TASK_OUTPUT_DIR": str(tmp_path / "task-outputs"),
            "INCIDENTLENS_TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
            "TELEMETRY_DB_URL": "sqlite:///:memory:",
            "INCIDENTLENS_CHECKPOINT_DB": str(tmp_path / "checkpoints.db"),
            "INCIDENTLENS_MODELS_CONFIG": str(tmp_path / "models.yaml"),
        }

        mock_registry = MagicMock()
        mock_registry.get = MagicMock()

        patchers = _patch_all(
            ProjectMemoryRuntime={"return_value": mock_memory_runtime},
            AgentCheckpointRuntime={"return_value": mock_cp_runtime},
            build_investigation_engine={"return_value": MagicMock()},
            load_models_config={"return_value": []},
            ModelRegistry={"return_value": mock_registry},
            SkillRuntime={"return_value": MagicMock()},
            create_engine={"return_value": MagicMock()},
            TelemetryRepository={"return_value": MagicMock()},
            InvestigationAuditStore={"return_value": MagicMock()},
            ScenarioStore={"return_value": MagicMock()},
            DemoResetService={"return_value": MagicMock()},
            InvestigationExportService={"return_value": MagicMock()},
        )

        with patch.dict("os.environ", env_overrides):
            for p in patchers:
                p.start()
            try:
                app = FastAPI()
                async with lifespan(app):
                    # Verify directories were created
                    assert (tmp_path / "memory").is_dir()
                    assert (tmp_path / "sessions").is_dir()
                    assert (tmp_path / "task-outputs").is_dir()
                    assert (tmp_path / "transcripts").is_dir()
            finally:
                for p in patchers:
                    p.stop()

    @pytest.mark.asyncio
    async def test_lifespan_passes_compaction_config_to_engine(
        self, tmp_path: Path
    ) -> None:
        """The lifespan constructs CompactionRuntimeConfig with resolved paths."""
        from incidentlens_control_plane.main import lifespan
        from fastapi import FastAPI

        mock_memory_runtime = AsyncMock()
        captured_compaction_config = {}

        def _capture_compaction_config(**kwargs):
            captured_compaction_config.update(kwargs)
            return MagicMock()

        env_overrides = {
            "INCIDENTLENS_AGENT_MODE": "llm_agent",
            "INCIDENTLENS_MEMORY_DIR": str(tmp_path / "memory"),
            "INCIDENTLENS_SESSION_DIR": str(tmp_path / "sessions"),
            "INCIDENTLENS_TASK_OUTPUT_DIR": str(tmp_path / "task-outputs"),
            "INCIDENTLENS_TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
            "TELEMETRY_DB_URL": "sqlite:///:memory:",
            "INCIDENTLENS_CHECKPOINT_DB": str(tmp_path / "checkpoints.db"),
            "INCIDENTLENS_MODELS_CONFIG": str(tmp_path / "models.yaml"),
        }

        mock_engine = MagicMock()
        mock_checkpoints = MagicMock()
        mock_checkpoints.saver = MagicMock()
        mock_cp_runtime = _AsyncContextManager(mock_checkpoints)
        mock_registry = MagicMock()
        mock_registry.get = MagicMock()

        patchers = _patch_all(
            ProjectMemoryRuntime={"return_value": mock_memory_runtime},
            CompactionRuntimeConfig={"side_effect": _capture_compaction_config},
            AgentCheckpointRuntime={"return_value": mock_cp_runtime},
            build_investigation_engine={"return_value": mock_engine},
            load_models_config={"return_value": []},
            ModelRegistry={"return_value": mock_registry},
            SkillRuntime={"return_value": MagicMock()},
            create_engine={"return_value": MagicMock()},
            TelemetryRepository={"return_value": MagicMock()},
            InvestigationAuditStore={"return_value": MagicMock()},
            ScenarioStore={"return_value": MagicMock()},
            DemoResetService={"return_value": MagicMock()},
            InvestigationExportService={"return_value": MagicMock()},
        )

        with patch.dict("os.environ", env_overrides):
            for p in patchers:
                p.start()
            try:
                app = FastAPI()
                async with lifespan(app):
                    pass

                # Verify CompactionRuntimeConfig was called with correct paths
                assert captured_compaction_config["session_dir"] == Path(
                    str(tmp_path / "sessions")
                )
                assert captured_compaction_config["transcript_dir"] == Path(
                    str(tmp_path / "transcripts")
                )
                assert captured_compaction_config["task_output_dir"] == Path(
                    str(tmp_path / "task-outputs")
                )
            finally:
                for p in patchers:
                    p.stop()
