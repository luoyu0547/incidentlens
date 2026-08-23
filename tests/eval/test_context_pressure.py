"""Pressure-evaluator tests: real compaction continuity and cost reduction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.store import AgentRound
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    CompactBoundary,
    Conclusion,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    ProviderUsage,
    StopReason,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

from eval.context_pressure import FinalState as final_state
from eval.context_pressure import PressureMetrics as metrics
from eval.context_pressure import evaluate_pressure
from eval.types import HarnessTrace

_NOW = datetime(2026, 8, 23, tzinfo=UTC)


def _now(second: int) -> datetime:
    return _NOW.replace(second=second)


def _events_path(tmp_path, events):
    path = tmp_path / "trace.jsonl"
    lines = [
        json.dumps(
            {
                "sequence": index,
                "occurred_at": "2026-08-23T00:00:00Z",
                "event_type": kind,
                "payload": payload,
            }
        )
        for index, (kind, payload) in enumerate(events, 1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def trace_with_compaction(tmp_path):
    return _events_path(
        tmp_path,
        [
            ("model_round.completed", {"round_number": 1, "input_tokens": 90_000}),
            ("context.compacted", {"through_sequence": 50}),
            ("model_round.completed", {"round_number": 2, "input_tokens": 18_000}),
            ("investigation.completed", {"status": "completed"}),
        ],
    )


def trace_no_compact_boundary(tmp_path):
    return _events_path(
        tmp_path,
        [
            ("model_round.completed", {"round_number": 1, "input_tokens": 90_000}),
            ("model_round.completed", {"round_number": 2, "input_tokens": 18_000}),
            ("investigation.completed", {"status": "completed"}),
        ],
    )


def trace_replays_history(tmp_path):
    return _events_path(
        tmp_path,
        [
            ("model_round.completed", {"round_number": 1, "input_tokens": 90_000}),
            ("context.compacted", {"through_sequence": 50}),
            ("model_round.completed", {"round_number": 2, "input_tokens": 88_000}),
            ("investigation.completed", {"status": "completed"}),
        ],
    )


def trace_task_incomplete(tmp_path):
    return _events_path(
        tmp_path,
        [
            ("model_round.completed", {"round_number": 1, "input_tokens": 90_000}),
            ("context.compacted", {"through_sequence": 50}),
            ("model_round.completed", {"round_number": 2, "input_tokens": 18_000}),
            ("investigation.failed", {"status": "failed"}),
        ],
    )


def test_pressure_run_requires_continuity_and_reduction(tmp_path) -> None:
    result = evaluate_pressure(
        trace_with_compaction(tmp_path),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(objective=True, todos=True, evidence=True, hashes=True),
    )
    assert result.passed


def test_pressure_accepts_dict_inputs(tmp_path) -> None:
    result = evaluate_pressure(
        trace_with_compaction(tmp_path),
        {"before_tokens": 90_000, "after_tokens": 18_000, "threshold_tokens": 60_000},
        {"objective": True, "todos": True, "evidence": True, "hashes": True},
    )
    assert result.passed


def test_threshold_not_crossed(tmp_path) -> None:
    result = evaluate_pressure(
        trace_with_compaction(tmp_path),
        metrics(before_tokens=5_000, after_tokens=1_000, threshold_tokens=8_000),
        final_state(),
    )
    assert not result.passed
    assert "threshold_not_crossed" in result.failures


def test_no_compact_boundary(tmp_path) -> None:
    result = evaluate_pressure(
        trace_no_compact_boundary(tmp_path),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(),
    )
    assert not result.passed
    assert "no_compact_boundary" in result.failures


def test_no_token_reduction(tmp_path) -> None:
    result = evaluate_pressure(
        trace_with_compaction(tmp_path),
        metrics(before_tokens=90_000, after_tokens=82_000),
        final_state(),
    )
    assert not result.passed
    assert "no_token_reduction" in result.failures


def test_preserved_state_lost(tmp_path) -> None:
    for flag in ("objective", "constraints", "todos", "evidence", "hashes"):
        result = evaluate_pressure(
            trace_with_compaction(tmp_path),
            metrics(before_tokens=90_000, after_tokens=18_000),
            final_state(**{flag: False}),
        )
        assert not result.passed
        assert f"{flag}_lost" in result.failures


def test_history_replayed(tmp_path) -> None:
    result = evaluate_pressure(
        trace_replays_history(tmp_path),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(),
    )
    assert not result.passed
    assert "history_replayed" in result.failures


def test_task_incomplete(tmp_path) -> None:
    result = evaluate_pressure(
        trace_task_incomplete(tmp_path),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(),
    )
    assert not result.passed
    assert "task_incomplete" in result.failures


def test_pressure_accepts_harness_trace() -> None:
    result = evaluate_pressure(
        _harness_trace(),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(),
    )
    assert result.passed


def test_pressure_accepts_live_recording() -> None:
    result = evaluate_pressure(
        _live_result(),
        metrics(before_tokens=90_000, after_tokens=18_000),
        final_state(),
    )
    assert result.passed


def _write_artifact(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_pressure_cli_pass_and_fail_exit_codes(tmp_path) -> None:
    import os
    import subprocess
    import sys

    _write_artifact(
        tmp_path / "metrics.json",
        {"before_tokens": 90_000, "after_tokens": 18_000, "threshold_tokens": 60_000},
    )
    _write_artifact(
        tmp_path / "manifest.json",
        {"objective": True, "todos": True, "evidence": True, "hashes": True},
    )
    _write_artifact(
        tmp_path / "fail-metrics.json",
        {"before_tokens": 5_000, "after_tokens": 1_000, "threshold_tokens": 8_000},
    )
    repo_root = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": repo_root}
    passing = subprocess.run(
        [sys.executable, "-m", "tests.eval.context_pressure",
         "--trace", str(trace_with_compaction(tmp_path)),
         "--metrics", str(tmp_path / "metrics.json"),
         "--manifest", str(tmp_path / "manifest.json")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert passing.returncode == 0, passing.stdout + passing.stderr
    failing = subprocess.run(
        [sys.executable, "-m", "tests.eval.context_pressure",
         "--trace", str(trace_with_compaction(tmp_path)),
         "--metrics", str(tmp_path / "fail-metrics.json"),
         "--manifest", str(tmp_path / "manifest.json")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert failing.returncode != 0
    assert "threshold_not_crossed" in failing.stdout
    missing = subprocess.run(
        [sys.executable, "-m", "tests.eval.context_pressure",
         "--trace", str(trace_with_compaction(tmp_path)),
         "--metrics", str(tmp_path / "metrics.json"),
         "--manifest", str(tmp_path / "missing.json")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2


# ---------------------------------------------------------------------------
# trace construction helpers
# ---------------------------------------------------------------------------


def _scope() -> AgentScope:
    return AgentScope(project_id="p", target_id="t", scope=LogScope.HOST)


def _evidence() -> tuple[EvidenceReference, ...]:
    return (EvidenceReference(evidence_id="ev-1", operation_id="op-1", summary="ok"),)


def _run() -> AgentRun:
    return AgentRun(
        agent_run_id="run-1",
        investigation_id="inv-1",
        kind=AgentRunKind.PARENT,
        scope=_scope(),
        status=AgentRunStatus.COMPLETED,
        budget=AgentBudget(),
        usage=UsageCounters(rounds=2, tool_calls=1),
        evidence=_evidence(),
        created_at=_now(0),
        updated_at=_now(3),
        started_at=_now(0),
        completed_at=_now(3),
    )


def _investigation() -> Investigation:
    return Investigation(
        investigation_id="inv-1",
        incident_id="incident-1",
        project_id="p",
        target_id="t",
        service="svc",
        symptom="down",
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=UsageCounters(rounds=2, tool_calls=1),
        stop_reason=StopReason.COMPLETED,
        created_at=_now(0),
        updated_at=_now(3),
    )


def _rounds() -> tuple[AgentRound, ...]:
    return (
        AgentRound(
            agent_run_id="run-1",
            round_number=1,
            status=AgentRunStatus.RUNNING,
            provider_usage=ProviderUsage(input_tokens=90_000),
            usage=UsageCounters(rounds=1),
            created_at=_now(0),
        ),
        AgentRound(
            agent_run_id="run-1",
            round_number=2,
            status=AgentRunStatus.RUNNING,
            provider_usage=ProviderUsage(input_tokens=16_000),
            usage=UsageCounters(rounds=2),
            created_at=_now(2),
        ),
    )


def _boundaries() -> tuple[CompactBoundary, ...]:
    return (
        CompactBoundary(
            agent_run_id="run-1",
            through_sequence=50,
            memory_revision=1,
            summary="compacted",
            created_at=_now(1),
        ),
    )


def _harness_trace() -> HarnessTrace:
    conclusions = (Conclusion(summary="fixed", evidence_ids=("ev-1",)),)
    return HarnessTrace(
        scenario="real_model",
        investigation=_investigation(),
        run=_run(),
        rounds=_rounds(),
        compact_boundaries=_boundaries(),
        evidence=_evidence(),
        conclusions=conclusions,
        conclusion_runs=tuple(("run-1", conclusion) for conclusion in conclusions),
        hook_events=(),
    )


class _LiveResult:
    """Minimal stand-in for ``record_live_model_demo.LiveModelRunResult``."""

    def __init__(
        self,
        *,
        run: dict[str, object],
        investigation: dict[str, object],
        rounds: tuple[dict[str, object], ...],
        compact_boundaries: tuple[dict[str, object], ...],
        evidence: tuple[dict[str, object], ...],
        conclusions: tuple[dict[str, object], ...],
    ) -> None:
        self.investigation = investigation
        self.run = run
        self.rounds = rounds
        self.transcript: tuple[dict[str, object], ...] = ()
        self.tool_calls: tuple[dict[str, object], ...] = ()
        self.compact_boundaries = compact_boundaries
        self.evidence = evidence
        self.conclusions = conclusions
        self.hooks: tuple[dict[str, object], ...] = ()
        self.report: dict[str, object] = {}

    def to_record(self) -> dict[str, object]:
        return {
            "investigation": self.investigation,
            "run": self.run,
            "rounds": list(self.rounds),
            "tool_calls": list(self.tool_calls),
            "evidence": list(self.evidence),
            "conclusions": list(self.conclusions),
            "report": self.report,
        }


def _live_result() -> _LiveResult:
    run_evidence = {"evidence_id": "ev-1", "operation_id": "op-1", "summary": "ok"}
    persisted_evidence = {
        "evidence_ref_id": "ev-1",
        "incident_id": "incident-1",
        "evidence_kind": "file_snapshot",
        "agent_run_id": "run-1",
        "project_id": "p",
        "target_id": "t",
        "service_name": "svc",
        "source_ref": "/workspace/service/live.log",
        "content_redacted": "bounded content preview",
        "content_sha256": "a" * 64,
        "redaction_summary": {"keys": 0},
        "metadata": {},
        "created_at": "2026-08-23T00:00:00Z",
        "created_by": "worker",
    }
    run = {
        "agent_run_id": "run-1",
        "investigation_id": "inv-1",
        "parent_run_id": None,
        "kind": "parent",
        "scope": {"project_id": "p", "target_id": "t", "scope": "host"},
        "status": "completed",
        "budget": {
            "max_rounds": 8,
            "max_tool_calls": 16,
            "max_wall_clock_seconds": 1800,
            "max_output_bytes_per_tool": 524288,
            "max_total_output_bytes": 4194304,
            "max_evidence": 100,
            "max_no_new_evidence_rounds": 3,
        },
        "usage": {
            "rounds": 2,
            "tool_calls": 1,
            "children": 0,
            "wall_clock_seconds": 0,
            "total_output_bytes": 0,
            "evidence_count": 1,
            "consecutive_no_new_evidence_rounds": 0,
        },
        "stop_reason": "completed",
        "evidence": [run_evidence],
        "hypotheses": [],
        "created_at": "2026-08-23T00:00:00Z",
        "updated_at": "2026-08-23T00:00:03Z",
        "started_at": "2026-08-23T00:00:00Z",
        "completed_at": "2026-08-23T00:00:03Z",
    }
    investigation = {
        "investigation_id": "inv-1",
        "incident_id": "incident-1",
        "project_id": "p",
        "target_id": "t",
        "service": "svc",
        "symptom": "down",
        "status": "completed",
        "budget": {
            "max_rounds": 32,
            "max_tool_calls": 64,
            "max_children": 4,
            "max_wall_clock_seconds": 7200,
            "max_total_output_bytes": 16777216,
            "max_evidence": 300,
            "max_no_new_evidence_rounds": 3,
        },
        "usage": {
            "rounds": 2,
            "tool_calls": 1,
            "children": 0,
            "wall_clock_seconds": 0,
            "total_output_bytes": 0,
            "evidence_count": 1,
            "consecutive_no_new_evidence_rounds": 0,
        },
        "stop_reason": "completed",
        "created_at": "2026-08-23T00:00:00Z",
        "updated_at": "2026-08-23T00:00:03Z",
        "started_at": "2026-08-23T00:00:00Z",
        "completed_at": "2026-08-23T00:00:03Z",
    }
    boundary = {
        "agent_run_id": "run-1",
        "through_sequence": 50,
        "memory_revision": 1,
        "summary": "compacted",
        "created_at": "2026-08-23T00:00:01Z",
    }
    rounds = (
        {
            "agent_run_id": "run-1",
            "round_number": 1,
            "status": "running",
            "provider_usage": {"input_tokens": 90_000, "output_tokens": 100, "output_bytes": 0},
            "usage": {
                "rounds": 1,
                "tool_calls": 0,
                "children": 0,
                "wall_clock_seconds": 0,
                "total_output_bytes": 0,
                "evidence_count": 0,
                "consecutive_no_new_evidence_rounds": 0,
            },
            "stop_reason": None,
            "created_at": "2026-08-23T00:00:00Z",
        },
        {
            "agent_run_id": "run-1",
            "round_number": 2,
            "status": "running",
            "provider_usage": {"input_tokens": 16_000, "output_tokens": 100, "output_bytes": 0},
            "usage": {
                "rounds": 2,
                "tool_calls": 0,
                "children": 0,
                "wall_clock_seconds": 0,
                "total_output_bytes": 0,
                "evidence_count": 0,
                "consecutive_no_new_evidence_rounds": 0,
            },
            "stop_reason": None,
            "created_at": "2026-08-23T00:00:02Z",
        },
    )
    return _LiveResult(
        run=run,
        investigation=investigation,
        rounds=rounds,
        compact_boundaries=(boundary,),
        evidence=(persisted_evidence,),
        conclusions=({"summary": "fixed", "evidence_ids": ("ev-1",)},),
    )
