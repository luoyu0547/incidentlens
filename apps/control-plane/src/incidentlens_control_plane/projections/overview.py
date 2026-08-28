"""Read-only overview projection over existing durable stores."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Callable

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.evidence.types import EvidenceKind
from incidentlens_control_plane.investigation.state_machine import INVESTIGATION_TERMINAL
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import InvestigationStatus
from incidentlens_control_plane.operations.store import OperationStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.projections.services import (
    ProjectionWindows,
    _aggregate_status,
    _issue_id,
    build_service_instances,
)
from incidentlens_control_plane.projections.types import (
    HealthStatus,
    OverviewServiceView,
    OverviewTargetView,
    OverviewView,
    ResolutionSummary,
    StatusCounts,
)
from incidentlens_control_plane.targets.service import TargetService
from incidentlens_control_plane.targets.store import TargetStore


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection timestamps must be timezone-aware")
    return value.astimezone(UTC)


class OverviewProjectionService:
    def __init__(
        self,
        *,
        target_service: TargetService,
        target_store: TargetStore,
        projects: ProjectRegistryStore,
        approvals: ApprovalStore,
        investigations: InvestigationStore,
        operations: OperationStore,
        logs,
        evidence: EvidenceStore,
        now: Callable[[], datetime] | None = None,
        windows: ProjectionWindows | None = None,
    ) -> None:
        self._target_service = target_service
        self._target_store = target_store
        self._projects = projects
        self._approvals = approvals
        self._investigations = investigations
        self._operations = operations
        self._logs = logs
        self._evidence = evidence
        self._now = now or (lambda: datetime.now(UTC))
        self._windows = windows or ProjectionWindows()

    def read_overview(
        self,
        *,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> OverviewView:
        generated_at = _utc(self._now())
        instances = build_service_instances(
            target_service=self._target_service,
            target_store=self._target_store,
            projects=self._projects,
            approvals=self._approvals,
            investigations=self._investigations,
            operations=self._operations,
            logs=self._logs,
            generated_at=generated_at,
            windows=self._windows,
            allowed_target_ids=allowed_target_ids,
        )
        grouped: dict[str, list] = {}
        for instance in instances:
            grouped.setdefault(instance.target.target_id, []).append(instance)

        targets = []
        for target in self._target_service.list_targets(now=generated_at):
            if allowed_target_ids is not None and target.target_id not in allowed_target_ids:
                continue
            target_instances = tuple(grouped.get(target.target_id, ()))
            if target_instances:
                target_status = _aggregate_status(
                    tuple(instance.status for instance in target_instances)
                )
                last_tested_at = max(
                    (
                        instance.last_tested_at
                        for instance in target_instances
                        if instance.last_tested_at is not None
                    ),
                    default=None,
                )
                last_observed_at = max(
                    (
                        instance.last_observed_at
                        for instance in target_instances
                        if instance.last_observed_at is not None
                    ),
                    default=None,
                )
            else:
                target_status = HealthStatus.UNKNOWN
                last_tested_at = None
                last_observed_at = None
            services = tuple(
                OverviewServiceView(
                    service_id=instance.registration.compose_service,
                    status=instance.status,
                    container_count=len(instance.registration.container_names),
                    open_issue_count=len(instance.issue_ids),
                    pending_approval_count=len(instance.pending_approval_ids),
                    last_observed_at=instance.last_observed_at,
                )
                for instance in sorted(
                    target_instances, key=lambda item: item.registration.compose_service
                )
            )
            targets.append(
                OverviewTargetView(
                    target_id=target.target_id,
                    name=target.name,
                    host=target.host,
                    status=target_status,
                    service_count=len(services),
                    services=services,
                    last_tested_at=last_tested_at,
                    last_observed_at=last_observed_at,
                )
            )
        targets.sort(key=lambda target: target.target_id)

        service_counter = Counter(instance.status for instance in instances)
        all_investigations = self._investigations.list_investigations()
        allowed_registry_targets = {
            (
                instance.binding.project_id,
                instance.binding.registry_target_id,
                instance.target.target_id,
            )
            for instance in instances
        }
        open_issues = [
            investigation
            for investigation in all_investigations
            if any(
                investigation.project_id == project_id
                and investigation.target_id == registry_target_id
                for project_id, registry_target_id, _ in allowed_registry_targets
            )
            and investigation.status
            not in {InvestigationStatus.COMPLETED, InvestigationStatus.CANCELLED}
        ]
        active_investigations = [
            investigation
            for investigation in open_issues
            if investigation.status not in INVESTIGATION_TERMINAL
        ]

        target_lookup = {
            (
                instance.binding.project_id,
                instance.binding.registry_target_id,
            ): instance.target.target_id
            for instance in instances
        }
        recent_resolutions = []
        resolution_cutoff = generated_at - self._windows.resolution_lookback
        for investigation in sorted(
            all_investigations,
            key=lambda item: item.completed_at or item.updated_at,
            reverse=True,
        ):
            if (
                investigation.status is not InvestigationStatus.COMPLETED
                or investigation.completed_at is None
                or investigation.completed_at < resolution_cutoff
                or (investigation.project_id, investigation.target_id) not in target_lookup
            ):
                continue
            conclusions = self._investigations.list_conclusions(
                investigation_id=investigation.investigation_id
            )
            validation_evidence = [
                evidence
                for evidence in self._evidence.list_for_incident(
                    investigation.incident_id, limit=1000
                )
                if evidence.evidence_kind is EvidenceKind.VALIDATION_RESULT
                and evidence.target_id == investigation.target_id
                and evidence.service_name == investigation.service
            ]
            latest_validation = (
                max(validation_evidence, key=lambda evidence: evidence.created_at)
                if validation_evidence
                else None
            )
            recent_resolutions.append(
                ResolutionSummary(
                    investigation_id=investigation.investigation_id,
                    issue_id=_issue_id(investigation.investigation_id),
                    target_id=target_lookup[(investigation.project_id, investigation.target_id)],
                    service_id=investigation.service,
                    symptom=investigation.symptom,
                    resolution_summary=(
                        conclusions[-1].summary if conclusions else investigation.symptom
                    ),
                    verification_summary=(
                        latest_validation.content_redacted
                        if latest_validation is not None
                        else None
                    ),
                    resolved_at=investigation.completed_at,
                )
            )
            if len(recent_resolutions) >= self._windows.max_recent_resolutions:
                break

        return OverviewView(
            generated_at=generated_at,
            targets=tuple(targets),
            service_counts=StatusCounts(
                healthy=service_counter[HealthStatus.HEALTHY],
                degraded=service_counter[HealthStatus.DEGRADED],
                unreachable=service_counter[HealthStatus.UNREACHABLE],
                unknown=service_counter[HealthStatus.UNKNOWN],
            ),
            open_issue_count=len({investigation.investigation_id for investigation in open_issues}),
            active_investigation_count=len(
                {investigation.investigation_id for investigation in active_investigations}
            ),
            pending_approval_count=sum(
                1
                for record in self._approvals.list(status=ApprovalStatus.PENDING)
                if record.target_id in set(target_lookup.values())
                or any(
                    record.target_id == registry_target_id
                    for _, registry_target_id, _ in allowed_registry_targets
                )
            ),
            recent_resolutions=tuple(recent_resolutions),
        )


__all__ = ["OverviewProjectionService", "ProjectionWindows"]
