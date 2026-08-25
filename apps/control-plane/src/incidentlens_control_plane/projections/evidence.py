"""Read-only evidence detail projection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.types import LogScope, LogSeverity, LogSourceKind
from incidentlens_control_plane.projections.issues import _log_cursor, _target_lookup
from incidentlens_control_plane.targets.service import TargetService
from incidentlens_control_plane.targets.store import TargetStore


class EvidenceProvenanceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref_id: str
    incident_id: str
    evidence_kind: EvidenceKind
    target_id: str
    service_id: str
    source_kind: LogSourceKind | None = None
    scope: LogScope | None = None
    severity: LogSeverity | None = None
    log_cursor: str | None = None
    created_at: datetime


class EvidenceDetailView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_redacted: str
    provenance: EvidenceProvenanceView


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection timestamps must be timezone-aware")
    return value.astimezone(UTC)


class EvidenceProjectionService:
    def __init__(
        self,
        *,
        target_service: TargetService,
        target_store: TargetStore,
        evidence: EvidenceStore,
        logs: LogStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._target_service = target_service
        self._target_store = target_store
        self._evidence = evidence
        self._logs = logs
        self._now = now or (lambda: datetime.now(UTC))

    def get_evidence(
        self,
        evidence_ref_id: str,
        *,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> EvidenceDetailView | None:
        try:
            evidence = self._evidence.get(evidence_ref_id)
        except KeyError:
            return None
        targets = _target_lookup(
            self._target_service,
            self._target_store,
            now=_utc(self._now()),
        )
        facade_target_id = targets.get((evidence.project_id, evidence.target_id))
        if facade_target_id is None:
            return None
        if allowed_target_ids is not None and facade_target_id not in allowed_target_ids:
            return None
        return EvidenceDetailView(
            content_redacted=evidence.content_redacted,
            provenance=EvidenceProvenanceView(
                evidence_ref_id=evidence.evidence_ref_id,
                incident_id=evidence.incident_id,
                evidence_kind=evidence.evidence_kind,
                target_id=facade_target_id,
                service_id=evidence.service_name,
                source_kind=evidence.source_kind,
                scope=evidence.scope,
                severity=evidence.severity,
                log_cursor=_log_cursor(self._logs, evidence),
                created_at=evidence.created_at,
            ),
        )


__all__ = ["EvidenceDetailView", "EvidenceProjectionService"]
