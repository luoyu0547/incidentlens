"""Evidence construction service for every evidence kind.

The service is the ONLY way run-scoped evidence beyond a stored log record is
created: each ``record_*`` method takes the raw, possibly sensitive content for
its kind and runs it through the redact -> bound/truncate -> hash pipeline
before persisting an immutable ``EvidenceRef``.  There is no "already redacted"
escape hatch — callers never hand the service text it stores verbatim, so a
model or API can never bypass the processing pipeline by claiming content is
already redacted.  Free-form metadata values are redacted and bounded too.

When an ``InvestigationStore`` is supplied, run-scoped methods also enforce
evidence ownership: the named ``agent_run_id`` must resolve to the incident and
match the evidence's project/target, so evidence can never be attributed across
investigations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.store import (
    AgentRunNotFound,
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.logs.redaction import redact_message
from incidentlens_control_plane.logs.types import (
    LogRecord,
    RedactionResult,
)


class EvidenceOwnershipError(Exception):
    """Raised when run-scoped evidence is attributed to the wrong incident."""


# Content caps, in characters.  Log messages and short summaries stay at the
# message cap; command output, file snapshots and diffs may go larger but are
# still bounded so raw content never reaches the store unbounded.
MESSAGE_MAX_LENGTH = 16 * 1024
CONTENT_MAX_LENGTH = 128 * 1024
SUMMARY_MAX_LENGTH = 16 * 1024
METADATA_VALUE_MAX_LENGTH = 2_000

_KIND_CONTENT_MAX: dict[EvidenceKind, int] = {
    EvidenceKind.COMMAND_OUTPUT: CONTENT_MAX_LENGTH,
    EvidenceKind.FILE_SNAPSHOT: CONTENT_MAX_LENGTH,
    EvidenceKind.DIFF: CONTENT_MAX_LENGTH,
    EvidenceKind.VALIDATION_RESULT: SUMMARY_MAX_LENGTH,
    EvidenceKind.CHILD_REPORT: SUMMARY_MAX_LENGTH,
    EvidenceKind.REGISTRY_DISCOVERY: SUMMARY_MAX_LENGTH,
    EvidenceKind.APPROVAL_DECISION: SUMMARY_MAX_LENGTH,
    EvidenceKind.UNCERTAIN_STATE: SUMMARY_MAX_LENGTH,
}


def _derive_evidence_ref_id(
    kind: EvidenceKind,
    *,
    project_id: str,
    target_id: str,
    service_name: str,
    agent_run_id: str | None,
    source_ref: str | None,
    metadata: dict[str, str],
    content_sha256: str,
) -> str:
    identity = "|".join(
        (project_id, target_id, service_name, agent_run_id or "", source_ref or "")
    )
    metadata_json = json.dumps(metadata, sort_keys=True)
    return "ev-" + hashlib.sha256(
        f"{kind.value}|{identity}|{metadata_json}|{content_sha256}".encode("utf-8")
    ).hexdigest()[:24]


class EvidenceService:
    """Construct bounded, redacted evidence and persist it idempotently."""

    def __init__(
        self,
        store: EvidenceStore,
        *,
        investigations: InvestigationStore | None = None,
    ) -> None:
        self._store = store
        self._investigations = investigations

    def from_log_record(
        self,
        record: LogRecord,
        incident_id: str,
        created_by: str,
        now: datetime,
        *,
        agent_run_id: str | None = None,
    ) -> EvidenceRef:
        """Create evidence from a stored redacted ``LogRecord``.

        When ``agent_run_id`` is set the ref is scoped to that run and its
        ownership is asserted against the investigation store (the same check
        the typed ``record_*`` methods run), so agent-collected log evidence is
        audited exactly like any other run evidence.  Without ``agent_run_id``
        this is the legacy incident-scoped path.
        """
        if agent_run_id is not None:
            self._assert_run_owned_by_incident(
                agent_run_id,
                incident_id,
                project_id=record.project_id,
                target_id=record.target_id,
                service_name=record.service_name,
            )
        return self._store.create_from_log_record(
            record,
            incident_id=incident_id,
            created_by=created_by,
            now=now,
            agent_run_id=agent_run_id,
        )

    def record_command_output(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str,
        command: str,
        output: str,
        exit_code: int,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted output of a shell command."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(output, EvidenceKind.COMMAND_OUTPUT)
        return self._persist(
            kind=EvidenceKind.COMMAND_OUTPUT,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={
                "command": self._bounded_redacted(command),
                "exit_code": str(exit_code),
            },
            created_by=created_by,
            now=now,
        )

    def record_file_snapshot(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str,
        content: str,
        size_bytes: int,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted snapshot of a file read."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(content, EvidenceKind.FILE_SNAPSHOT)
        return self._persist(
            kind=EvidenceKind.FILE_SNAPSHOT,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={"size_bytes": str(size_bytes)},
            created_by=created_by,
            now=now,
        )

    def record_diff(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str,
        diff_text: str,
        operation: str,
        old_ref: str,
        new_ref: str,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted diff between two states."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(diff_text, EvidenceKind.DIFF)
        return self._persist(
            kind=EvidenceKind.DIFF,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={
                "operation": self._bounded_redacted(operation),
                "old_ref": self._bounded_redacted(old_ref),
                "new_ref": self._bounded_redacted(new_ref),
            },
            created_by=created_by,
            now=now,
        )

    def record_validation_result(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str,
        validator: str,
        passed: bool,
        detail: str,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted outcome of a validation check."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(detail, EvidenceKind.VALIDATION_RESULT)
        return self._persist(
            kind=EvidenceKind.VALIDATION_RESULT,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={
                "validator": self._bounded_redacted(validator),
                "passed": str(passed).lower(),
            },
            created_by=created_by,
            now=now,
        )

    def record_child_report(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str,
        report_summary: str,
        child_run_id: str,
        parent_run_id: str,
        status: str,
        stop_reason: str,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted report a child run returned."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(report_summary, EvidenceKind.CHILD_REPORT)
        return self._persist(
            kind=EvidenceKind.CHILD_REPORT,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={
                "child_run_id": self._bounded_redacted(child_run_id),
                "parent_run_id": self._bounded_redacted(parent_run_id),
                "status": self._bounded_redacted(status),
                "stop_reason": self._bounded_redacted(stop_reason),
            },
            created_by=created_by,
            now=now,
        )

    def record_registry_discovery(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str,
        discovery_kind: str,
        description: str,
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted discovery of a registry extension."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(
            description, EvidenceKind.REGISTRY_DISCOVERY
        )
        return self._persist(
            kind=EvidenceKind.REGISTRY_DISCOVERY,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={"discovery_kind": self._bounded_redacted(discovery_kind)},
            created_by=created_by,
            now=now,
        )

    def record_approval_decision(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        approval_id: str,
        decision: str,
        intent_summary: str,
        source_ref: str | None = None,
        created_by: str = "service",
        now: datetime,
    ) -> EvidenceRef:
        """Record the bounded, redacted outcome of an approval decision."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(
            intent_summary, EvidenceKind.APPROVAL_DECISION
        )
        return self._persist(
            kind=EvidenceKind.APPROVAL_DECISION,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={
                "approval_id": self._bounded_redacted(approval_id),
                "decision": self._bounded_redacted(decision),
            },
            created_by=created_by,
            now=now,
        )

    def record_uncertain_state(
        self,
        *,
        agent_run_id: str,
        incident_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        reason: str,
        description: str,
        source_ref: str | None = None,
        created_by: str = "service",
        now: datetime,
    ) -> EvidenceRef:
        """Record that a run stopped on an uncertain state."""
        self._assert_run_owned_by_incident(
            agent_run_id,
            incident_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
        )
        redacted = self._redact_content(description, EvidenceKind.UNCERTAIN_STATE)
        return self._persist(
            kind=EvidenceKind.UNCERTAIN_STATE,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            redacted=redacted,
            metadata={"reason": self._bounded_redacted(reason)},
            created_by=created_by,
            now=now,
        )

    # --- internals ---

    def _redact_content(
        self, content: str, kind: EvidenceKind
    ) -> RedactionResult:
        return redact_message(content, max_length=_KIND_CONTENT_MAX[kind])

    def _bounded_redacted(self, text: str) -> str:
        return redact_message(
            text, max_length=METADATA_VALUE_MAX_LENGTH
        ).message_redacted

    def _persist(
        self,
        *,
        kind: EvidenceKind,
        incident_id: str,
        agent_run_id: str,
        project_id: str,
        target_id: str,
        service_name: str,
        source_ref: str | None,
        redacted: RedactionResult,
        metadata: dict[str, str],
        created_by: str,
        now: datetime,
    ) -> EvidenceRef:
        now_utc = now.astimezone(UTC)
        content_sha256 = hashlib.sha256(
            redacted.message_redacted.encode("utf-8")
        ).hexdigest()
        evidence_ref_id = _derive_evidence_ref_id(
            kind,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            agent_run_id=agent_run_id,
            source_ref=source_ref,
            metadata=metadata,
            content_sha256=content_sha256,
        )
        evidence = EvidenceRef(
            evidence_ref_id=evidence_ref_id,
            incident_id=incident_id,
            evidence_kind=kind,
            agent_run_id=agent_run_id,
            project_id=project_id,
            target_id=target_id,
            service_name=service_name,
            source_ref=source_ref,
            content_redacted=redacted.message_redacted,
            content_sha256=content_sha256,
            redaction_summary=redacted.summary,
            truncation=redacted.truncation,
            metadata=metadata,
            created_at=now_utc,
            created_by=created_by,
        )
        return self._store.create(evidence)

    def _assert_run_owned_by_incident(
        self,
        agent_run_id: str,
        incident_id: str,
        *,
        project_id: str,
        target_id: str,
        service_name: str,
    ) -> None:
        if self._investigations is None:
            return
        try:
            run = self._investigations.get_agent_run(agent_run_id)
            investigation = self._investigations.get_investigation(
                run.investigation_id
            )
        except (AgentRunNotFound, InvestigationNotFound) as exc:
            raise EvidenceOwnershipError(
                f"run {agent_run_id} is not registered for incident {incident_id}"
            ) from exc
        if investigation.incident_id != incident_id:
            raise EvidenceOwnershipError(
                f"run {agent_run_id} belongs to incident "
                f"{investigation.incident_id}, not {incident_id}"
            )
        if (
            run.scope.project_id != project_id
            or run.scope.target_id != target_id
        ):
            raise EvidenceOwnershipError(
                f"run {agent_run_id} scope is "
                f"{run.scope.project_id}/{run.scope.target_id}, not "
                f"{project_id}/{target_id}"
            )
        if (
            run.scope.service_name is not None
            and run.scope.service_name != service_name
        ):
            raise EvidenceOwnershipError(
                f"run {agent_run_id} scope service is "
                f"{run.scope.service_name}, not {service_name}"
            )
