"""ReportService tests: aggregate investigation + evidence into both formats."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    HypothesisStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Conclusion,
    Hypothesis,
    Investigation,
    InvestigationBudget,
    ToolCall,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.reports.service import ReportService
from incidentlens_control_plane.reports.types import ReportBundle


@pytest.fixture()
def stores(tmp_path: Path):
    def connect():
        return sqlite3.connect(tmp_path / "runtime.db")

    inv_store = InvestigationStore(connect)
    ev_store = EvidenceStore(connect)
    inv_store.migrate()
    ev_store.migrate()
    return inv_store, ev_store


def _create_investigation(store: InvestigationStore) -> str:
    inv = Investigation(
        investigation_id="inv-test001",
        incident_id="inc-test001",
        project_id="proj-1",
        target_id="target-1",
        service="order-service",
        symptom="timeout errors under load",
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 13, 10, 5, 0, tzinfo=UTC),
    )
    store.create_investigation(inv)
    return inv.investigation_id


def _seed_aggregation(
    inv_store: InvestigationStore,
    ev_store: EvidenceStore,
    investigation_id: str,
) -> None:
    """Seed one run/tool-call/hypothesis/conclusion/evidence via the real stores.

    Exercises every aggregation branch in ``ReportService._build_sections``:
    timeline (run kind/status, tool-call started_at), evidence summary,
    hypotheses, root-cause (latest conclusion summary), and the appendix
    tool-call evidence-id join.
    """
    now = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)
    inv_store.create_agent_run(
        AgentRun(
            agent_run_id="run00000001",
            investigation_id=investigation_id,
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=AgentScope(
                project_id="proj-1", target_id="target-1", scope=LogScope.HOST
            ),
            status=AgentRunStatus.COMPLETED,
            budget=AgentBudget(),
            usage=UsageCounters(),
            created_at=now,
            updated_at=now,
        )
    )
    inv_store.create_tool_call(
        ToolCall(
            tool_call_id="call-test001",
            agent_run_id="run00000001",
            tool_name="log_tail",
            status=ToolCallStatus.SUCCEEDED,
            idempotency_key="call-test001",
            planned_at=now,
            started_at=now,
            evidence_ids=("ev-test001",),
        )
    )
    inv_store.create_hypothesis(
        Hypothesis(
            hypothesis_id="hyp-test001",
            agent_run_id="run00000001",
            summary="connection pool exhaustion",
            status=HypothesisStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
    )
    inv_store.create_conclusion(
        "run00000001",
        investigation_id,
        Conclusion(
            summary="connection pool exhaustion caused by leaked DB connections",
            evidence_ids=("ev-test001",),
        ),
        now=now,
    )
    ev_store.create(
        EvidenceRef(
            evidence_ref_id="ev-test001",
            incident_id="inc-test001",
            evidence_kind=EvidenceKind.LOG_RECORD,
            agent_run_id="run00000001",
            project_id="proj-1",
            target_id="target-1",
            service_name="order-service",
            source_ref="/var/log/order/app.log",
            content_redacted="[REDACTED] connection pool exhausted",
            content_sha256="0" * 64,
            redaction_summary={},
            created_at=now,
            created_by="run00000001",
        )
    )


def test_generate_creates_both_files(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)
    assert isinstance(bundle, ReportBundle)
    assert bundle.investigation_id == inv_id
    assert bundle.markdown_path.exists()
    assert bundle.html_path.exists()
    assert bundle.markdown_path.read_text().startswith("# Investigation Report")
    assert "<!DOCTYPE html>" in bundle.html_path.read_text()


def test_generate_investigation_not_found(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    with pytest.raises(KeyError):
        svc.generate("nonexistent")


def test_generate_metadata_matches_investigation(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)
    assert bundle.metadata.symptom == "timeout errors under load"
    assert "order-service" in bundle.metadata.services_affected


def test_generate_aggregates_runs_tool_calls_hypotheses_and_evidence(
    stores, tmp_path
) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    _seed_aggregation(inv_store, ev_store, inv_id)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)

    # 聚合后的元数据：证据数、工具调用数与根因摘要来自真实 store 数据。
    assert bundle.metadata.evidence_count == 1
    assert bundle.metadata.tool_calls_count == 1
    assert bundle.metadata.root_cause == (
        "connection pool exhaustion caused by leaked DB connections"
    )

    md = bundle.markdown_path.read_text()
    html = bundle.html_path.read_text()

    # 根因分析（最新 conclusion.summary）
    assert "connection pool exhaustion caused by leaked DB connections" in md
    assert "connection pool exhaustion caused by leaked DB connections" in html
    # 时间线：run 的 kind + 截断 run id + 终止状态；tool call 的 started_at + 状态
    assert "`00000001` started (parent)" in md
    assert "Finished: completed" in md
    assert "`log_tail` → succeeded" in md
    # 证据汇总：evidence_kind.value / evidence_ref_id / source_ref
    assert "[log_record] ev-test001: /var/log/order/app.log" in md
    # 假设演进：hypothesis.summary + status.value
    assert "**confirmed**: connection pool exhaustion" in md
    # 附录：tool call 的 evidence_ids join
    assert "`log_tail` (succeeded) — evidence: ev-test001" in md
