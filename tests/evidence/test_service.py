"""Tests for the bounded/redacted EvidenceService construction pipeline."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.evidence.service import (
    CONTENT_MAX_LENGTH,
    EvidenceOwnershipError,
    EvidenceService,
)
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


def make_store(tmp_path: Path) -> EvidenceStore:
    store = EvidenceStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    return store


def make_investigation_store(
    tmp_path: Path,
    *,
    incident_id: str = "inc-1",
    run_id: str = "run-1",
    project_id: str = "payments",
    target_id: str = "dev-a",
    scope_kind: LogScope = LogScope.HOST,
    service_name: str | None = None,
    container_name: str | None = None,
) -> InvestigationStore:
    inv_store = InvestigationStore(
        lambda: sqlite3.connect(tmp_path / "investigations.db")
    )
    inv_store.migrate()
    if scope_kind is LogScope.CONTAINER:
        scope = AgentScope(
            project_id=project_id,
            target_id=target_id,
            scope=LogScope.CONTAINER,
            service_name=service_name or "orders",
            container_name=container_name or "orders-1",
        )
    else:
        scope = AgentScope(
            project_id=project_id, target_id=target_id, scope=LogScope.HOST
        )
    inv_store.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id=incident_id,
            project_id=project_id,
            target_id=target_id,
            service="payment-api",
            symptom="checkout errors",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    inv_store.create_agent_run(
        AgentRun(
            agent_run_id=run_id,
            investigation_id="inv-1",
            parent_run_id=None,
            kind=AgentRunKind.PARENT,
            scope=scope,
            status=AgentRunStatus.RUNNING,
            budget=AgentBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return inv_store


def _command_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "agent_run_id": "run-1",
        "incident_id": "inc-1",
        "project_id": "payments",
        "target_id": "dev-a",
        "service_name": "payment-api",
        "source_ref": "host:dev-a",
        "command": "systemctl restart mysql",
        "output": "restarting mysql token=abc123",
        "exit_code": 0,
        "created_by": "agent",
        "now": NOW,
    }
    kwargs.update(overrides)
    return kwargs


def test_command_output_redacts_and_bounds(tmp_path: Path) -> None:
    service = EvidenceService(make_store(tmp_path))
    ref = service.record_command_output(
        **_command_kwargs(
            command="cat /opt/.env password=hunter2",
            output="restarting token=abc123 at 10.1.2.3",
        )
    )

    assert ref.evidence_kind == EvidenceKind.COMMAND_OUTPUT
    assert ref.agent_run_id == "run-1"
    assert "abc123" not in ref.content_redacted
    assert "10.1.2.3" not in ref.content_redacted
    assert "hunter2" not in ref.metadata["command"]
    assert ref.metadata["exit_code"] == "0"
    assert ref.redaction_summary["token"] == 1
    assert ref.content_sha256 == hashlib.sha256(
        ref.content_redacted.encode("utf-8")
    ).hexdigest()


def test_content_never_equals_raw_input(tmp_path: Path) -> None:
    """The service stores redacted content, never the raw text it was given."""
    service = EvidenceService(make_store(tmp_path))
    raw = "token=abc123 secret=hunter2"
    ref = service.record_command_output(**_command_kwargs(output=raw))

    assert ref.content_redacted != raw
    assert "abc123" not in ref.content_redacted
    assert "hunter2" not in ref.content_redacted
    assert ref.model_dump_json() != raw


def test_large_content_truncated_with_summary(tmp_path: Path) -> None:
    service = EvidenceService(make_store(tmp_path))
    output = "x" * (CONTENT_MAX_LENGTH + 100)
    ref = service.record_command_output(**_command_kwargs(output=output))

    assert len(ref.content_redacted) <= CONTENT_MAX_LENGTH
    assert ref.truncation is not None
    assert ref.truncation.truncated is True
    assert ref.truncation.original_length == CONTENT_MAX_LENGTH + 100
    assert ref.truncation.kept_length == CONTENT_MAX_LENGTH
    assert ref.redaction_summary.get("truncated") == 1
    # The hash is over the stored (redacted + truncated) content.
    assert ref.content_sha256 == hashlib.sha256(
        ref.content_redacted.encode("utf-8")
    ).hexdigest()


def test_no_pre_redacted_escape_hatch(tmp_path: Path) -> None:
    """A caller cannot bypass the pipeline by claiming text is already redacted."""
    service = EvidenceService(make_store(tmp_path))
    # The service has no generic create-from-text method; passing a value that
    # still contains a secret must never persist that secret.
    ref = service.record_command_output(
        **_command_kwargs(output="token=abc123 message looks clean")
    )
    assert "abc123" not in ref.content_redacted
    assert ref.content_redacted == "token=[REDACTED_TOKEN] message looks clean"


def test_all_kinds_build_bounded_evidence(tmp_path: Path) -> None:
    service = EvidenceService(make_store(tmp_path))
    base = {
        "agent_run_id": "run-1",
        "incident_id": "inc-1",
        "project_id": "payments",
        "target_id": "dev-a",
        "service_name": "payment-api",
        "source_ref": "host:dev-a",
        "created_by": "agent",
        "now": NOW,
    }
    refs = [
        service.record_command_output(
            **base, command="ls", output="ok token=abc", exit_code=0
        ),
        service.record_file_snapshot(
            **base, content="config password=hunter2", size_bytes=10
        ),
        service.record_diff(
            **base,
            diff_text="--- a\n+++ b\npassword=hunter2",
            operation="edit",
            old_ref="old",
            new_ref="new",
        ),
        service.record_validation_result(
            **base, validator="syntax", passed=True, detail="ok token=abc"
        ),
        service.record_child_report(
            **base,
            report_summary="child complete token=abc",
            child_run_id="child-1",
            parent_run_id="run-1",
            status="complete",
            stop_reason="completed",
        ),
        service.record_registry_discovery(
            **base, discovery_kind="container", description="found payments-api-1 token=abc"
        ),
        service.record_approval_decision(
            **base,
            approval_id="ap-1",
            decision="approved",
            intent_summary="restart container token=abc",
        ),
        service.record_uncertain_state(
            **base, reason="ambiguous", description="cannot decide token=abc"
        ),
    ]

    assert {r.evidence_kind for r in refs} == {
        EvidenceKind.COMMAND_OUTPUT,
        EvidenceKind.FILE_SNAPSHOT,
        EvidenceKind.DIFF,
        EvidenceKind.VALIDATION_RESULT,
        EvidenceKind.CHILD_REPORT,
        EvidenceKind.REGISTRY_DISCOVERY,
        EvidenceKind.APPROVAL_DECISION,
        EvidenceKind.UNCERTAIN_STATE,
    }
    for ref in refs:
        assert "abc" not in ref.content_redacted
        assert "hunter2" not in ref.content_redacted
        assert ref.agent_run_id == "run-1"
        assert ref.source_kind is None
        assert ref.scope is None
        assert ref.cursor is None
        assert ref.content_sha256 == hashlib.sha256(
            ref.content_redacted.encode("utf-8")
        ).hexdigest()


def test_idempotent_recreation_returns_same_ref(tmp_path: Path) -> None:
    service = EvidenceService(make_store(tmp_path))
    kwargs = _command_kwargs()
    first = service.record_command_output(**kwargs)
    second = service.record_command_output(**kwargs)

    assert second.evidence_ref_id == first.evidence_ref_id
    assert service._store.query(
        incident_id="inc-1", evidence_kind=EvidenceKind.COMMAND_OUTPUT
    ) == (first,)


def test_identical_output_across_runs_is_distinct(tmp_path: Path) -> None:
    """Same command output from a different run must not collapse to one ref."""
    service = EvidenceService(make_store(tmp_path))
    first = service.record_command_output(**_command_kwargs(agent_run_id="run-1"))
    second = service.record_command_output(**_command_kwargs(agent_run_id="run-2"))

    assert first.evidence_ref_id != second.evidence_ref_id
    assert {r.agent_run_id for r in service._store.query(
        incident_id="inc-1", evidence_kind=EvidenceKind.COMMAND_OUTPUT
    )} == {"run-1", "run-2"}


def test_ownership_rejected_for_foreign_incident(tmp_path: Path) -> None:
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(tmp_path, incident_id="inc-999"),
    )
    with pytest.raises(EvidenceOwnershipError):
        service.record_command_output(**_command_kwargs())


def test_ownership_rejected_for_unknown_run(tmp_path: Path) -> None:
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(tmp_path),
    )
    with pytest.raises(EvidenceOwnershipError):
        service.record_command_output(**_command_kwargs(agent_run_id="ghost"))


def test_ownership_rejected_for_scope_mismatch(tmp_path: Path) -> None:
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(tmp_path),
    )
    with pytest.raises(EvidenceOwnershipError):
        service.record_command_output(
            **_command_kwargs(project_id="other-project")
        )


def test_ownership_allowed_for_matching_run(tmp_path: Path) -> None:
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(tmp_path),
    )
    ref = service.record_command_output(**_command_kwargs())
    assert ref.incident_id == "inc-1"
    assert ref.agent_run_id == "run-1"


def test_ownership_skipped_without_investigation_store(tmp_path: Path) -> None:
    service = EvidenceService(make_store(tmp_path))
    ref = service.record_command_output(**_command_kwargs(agent_run_id="ghost"))
    assert ref.agent_run_id == "ghost"


def test_same_content_different_metadata_is_distinct(tmp_path: Path) -> None:
    """Same redacted content under different metadata must not collapse."""
    service = EvidenceService(make_store(tmp_path))
    first = service.record_command_output(
        **_command_kwargs(command="systemctl restart mysql")
    )
    second = service.record_command_output(
        **_command_kwargs(command="systemctl status mysql")
    )

    assert first.content_redacted == second.content_redacted
    assert first.evidence_ref_id != second.evidence_ref_id
    assert len(
        service._store.query(
            incident_id="inc-1", evidence_kind=EvidenceKind.COMMAND_OUTPUT
        )
    ) == 2


def test_ownership_rejected_for_service_mismatch(tmp_path: Path) -> None:
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(
            tmp_path, scope_kind=LogScope.CONTAINER, service_name="orders"
        ),
    )
    with pytest.raises(EvidenceOwnershipError):
        service.record_command_output(
            **_command_kwargs(service_name="payment-api")
        )


def test_ownership_allowed_for_matching_container_service(tmp_path: Path) -> None:
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(
            tmp_path, scope_kind=LogScope.CONTAINER, service_name="orders"
        ),
    )
    ref = service.record_command_output(
        **_command_kwargs(service_name="orders")
    )
    assert ref.service_name == "orders"


def test_ownership_skips_service_check_for_host_scope(tmp_path: Path) -> None:
    """Host-scoped runs have no service in their scope; the check is skipped."""
    service = EvidenceService(
        make_store(tmp_path),
        investigations=make_investigation_store(tmp_path),
    )
    ref = service.record_command_output(**_command_kwargs(service_name="payment-api"))
    assert ref.service_name == "payment-api"
