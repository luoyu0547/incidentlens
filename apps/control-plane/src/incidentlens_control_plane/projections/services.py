"""Read-only service projection over existing durable stores."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.approvals.types import ApprovalRecord, ApprovalStatus
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import Investigation
from incidentlens_control_plane.logs.store import LogStore, _record_from_row
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogSeverity,
    LogSubscription,
    LogSubscriptionStatus,
)
from incidentlens_control_plane.operations.store import OperationStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import ServiceRegistration
from incidentlens_control_plane.projections.types import (
    HealthStatus,
    LogSourceSummary,
    ProjectionWindows,
    ServiceDetailView,
    ServiceInstanceView,
)
from incidentlens_control_plane.targets.service import TargetService
from incidentlens_control_plane.targets.store import TargetStore
from incidentlens_control_plane.targets.types import TargetBinding, TargetView

_REACHABLE_RE = re.compile(r"\breachable=(True|False)\b")
_NEGATIVE_INVESTIGATION_STATUSES = frozenset(
    {
        InvestigationStatus.FAILED,
        InvestigationStatus.PAUSED_UNCERTAIN_STATE,
    }
)


@dataclass(frozen=True, slots=True)
class _TargetTestSignal:
    status: str
    reachable: bool | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _ServiceInstanceProjection:
    target: TargetView
    binding: TargetBinding
    registration: ServiceRegistration
    status: HealthStatus
    last_tested_at: datetime | None
    last_observed_at: datetime | None
    issue_ids: tuple[str, ...]
    investigation_ids: tuple[str, ...]
    pending_approval_ids: tuple[str, ...]
    subscriptions: tuple[LogSubscription, ...]
    records: tuple[LogRecord, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _issue_id(investigation_id: str) -> str:
    return f"iss_{investigation_id}"


def _max_datetime(values: tuple[datetime | None, ...]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _aggregate_status(statuses: tuple[HealthStatus, ...]) -> HealthStatus:
    if any(status is HealthStatus.UNREACHABLE for status in statuses):
        return HealthStatus.UNREACHABLE
    if any(status is HealthStatus.DEGRADED for status in statuses):
        return HealthStatus.DEGRADED
    if statuses and all(status is HealthStatus.HEALTHY for status in statuses):
        return HealthStatus.HEALTHY
    return HealthStatus.UNKNOWN


def _approval_matches_target(
    record: ApprovalRecord,
    *,
    facade_target_id: str,
    service_name: str,
    investigation_ids: frozenset[str],
) -> bool:
    if record.service not in {None, service_name}:
        return False
    if record.target_id == facade_target_id:
        return True
    return record.investigation_id in investigation_ids


def _parse_reachable(progress_summary: str | None) -> bool | None:
    if not progress_summary:
        return None
    match = _REACHABLE_RE.search(progress_summary)
    if match is None:
        return None
    return match.group(1) == "True"


def _latest_target_tests(
    store: OperationStore,
    *,
    target_ids: tuple[str, ...],
) -> dict[str, _TargetTestSignal]:
    if not target_ids:
        return {}
    placeholders = ", ".join("?" for _ in target_ids)
    connection_factory: Callable[[], sqlite3.Connection] = store._connection_factory
    with connection_factory() as conn:
        rows = conn.execute(
            f"""
            SELECT target_id, status, progress_summary,
                   COALESCE(finished_at, updated_at, created_at) AS observed_at
            FROM operations
            WHERE kind = ? AND target_id IN ({placeholders})
            ORDER BY observed_at DESC
            """,
            ("target_test", *target_ids),
        ).fetchall()
    latest: dict[str, _TargetTestSignal] = {}
    for target_id, status, progress_summary, observed_at in rows:
        key = str(target_id)
        if key in latest:
            continue
        progress_text = (
            str(progress_summary) if progress_summary is not None else None
        )
        latest[key] = _TargetTestSignal(
            status=str(status),
            reachable=_parse_reachable(progress_text),
            observed_at=datetime.fromisoformat(str(observed_at)).astimezone(UTC),
        )
    return latest


def _recent_error_records(
    store: LogStore,
    *,
    project_id: str,
    target_id: str,
    service_name: str,
    observed_after: datetime,
) -> tuple[LogRecord, ...]:
    connection_factory: Callable[[], sqlite3.Connection] = store._connection_factory
    with connection_factory() as conn:
        rows = conn.execute(
            """
            SELECT log_id, subscription_id, project_id, target_id, service_name,
                   source_kind, scope, source_ref, cursor, dedupe_key,
                   observed_at, event_time, severity, message_redacted,
                   redaction_summary_json, normal_signal, correlation_key,
                   evidence_ref_id, created_at
            FROM log_records
            WHERE project_id = ?
              AND target_id = ?
              AND service_name = ?
              AND observed_at >= ?
              AND severity IN (?, ?)
            ORDER BY observed_at DESC
            """,
            (
                project_id,
                target_id,
                service_name,
                observed_after.isoformat(),
                LogSeverity.ERROR.value,
                LogSeverity.CRITICAL.value,
            ),
        ).fetchall()
    return tuple(_record_from_row(row) for row in rows)


def _latest_log_observed_at(
    store: LogStore,
    *,
    project_id: str,
    target_id: str,
    service_name: str,
    source_kind: str | None = None,
    scope: str | None = None,
) -> datetime | None:
    connection_factory: Callable[[], sqlite3.Connection] = store._connection_factory
    clauses = [
        "project_id = ?",
        "target_id = ?",
        "service_name = ?",
    ]
    params: list[object] = [project_id, target_id, service_name]
    if source_kind is not None:
        clauses.append("source_kind = ?")
        params.append(source_kind)
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    where_sql = " AND ".join(clauses)
    with connection_factory() as conn:
        row = conn.execute(
            f"""
            SELECT MAX(observed_at)
            FROM log_records
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return datetime.fromisoformat(str(row[0])).astimezone(UTC)


def _derive_service_status(
    *,
    generated_at: datetime,
    windows: ProjectionWindows,
    target_test: _TargetTestSignal | None,
    subscriptions: tuple[LogSubscription, ...],
    records: tuple[LogRecord, ...],
    investigations: tuple[Investigation, ...],
) -> HealthStatus:
    target_cutoff = generated_at - windows.target_test_lookback
    subscription_cutoff = generated_at - windows.subscription_lookback

    if target_test is not None and target_test.observed_at >= target_cutoff:
        if target_test.status in {"failed", "uncertain"} or target_test.reachable is False:
            return HealthStatus.UNREACHABLE

    if any(
        investigation.status in _NEGATIVE_INVESTIGATION_STATUSES
        for investigation in investigations
    ):
        return HealthStatus.DEGRADED
    if any(
        subscription.status is LogSubscriptionStatus.ERROR
        and subscription.updated_at >= subscription_cutoff
        for subscription in subscriptions
    ):
        return HealthStatus.DEGRADED
    if records:
        return HealthStatus.DEGRADED

    if (
        target_test is not None
        and target_test.observed_at >= target_cutoff
        and target_test.reachable is True
    ):
        return HealthStatus.HEALTHY
    if any(
        subscription.status is LogSubscriptionStatus.ACTIVE
        and subscription.updated_at >= subscription_cutoff
        for subscription in subscriptions
    ):
        return HealthStatus.HEALTHY
    return HealthStatus.UNKNOWN


def build_service_instances(
    *,
    target_service: TargetService,
    target_store: TargetStore,
    projects: ProjectRegistryStore,
    approvals: ApprovalStore,
    investigations: InvestigationStore,
    operations: OperationStore,
    logs: LogStore,
    generated_at: datetime,
    windows: ProjectionWindows,
    allowed_target_ids: frozenset[str] | None = None,
    service_id: str | None = None,
) -> tuple[_ServiceInstanceProjection, ...]:
    targets = tuple(
        target
        for target in target_service.list_targets(now=generated_at)
        if allowed_target_ids is None or target.target_id in allowed_target_ids
    )
    latest_tests = _latest_target_tests(
        operations,
        target_ids=tuple(target.target_id for target in targets),
    )
    pending_approvals = approvals.list(status=ApprovalStatus.PENDING)
    all_investigations = investigations.list_investigations()

    projections: list[_ServiceInstanceProjection] = []
    for target in targets:
        binding = target_store.get(target.target_id)
        project = projects.get(binding.project_id)
        for registration in project.services:
            if service_id is not None and registration.compose_service != service_id:
                continue
            matching_investigations = tuple(
                investigation
                for investigation in all_investigations
                if investigation.project_id == binding.project_id
                and investigation.target_id == binding.registry_target_id
                and investigation.service == registration.compose_service
            )
            subscriptions = logs.list_subscriptions(
                project_id=binding.project_id,
                target_id=binding.registry_target_id,
                service_name=registration.compose_service,
            )
            recent_errors = _recent_error_records(
                logs,
                project_id=binding.project_id,
                target_id=binding.registry_target_id,
                service_name=registration.compose_service,
                observed_after=generated_at - windows.error_lookback,
            )
            investigation_ids = frozenset(
                investigation.investigation_id
                for investigation in matching_investigations
            )
            pending_approval_ids = tuple(
                record.approval_id
                for record in pending_approvals
                if _approval_matches_target(
                    record,
                    facade_target_id=target.target_id,
                    service_name=registration.compose_service,
                    investigation_ids=investigation_ids,
                )
            )
            issue_ids = tuple(
                _issue_id(investigation.investigation_id)
                for investigation in matching_investigations
                if investigation.status
                not in {InvestigationStatus.COMPLETED, InvestigationStatus.CANCELLED}
            )
            status = _derive_service_status(
                generated_at=generated_at,
                windows=windows,
                target_test=latest_tests.get(target.target_id),
                subscriptions=subscriptions,
                records=recent_errors,
                investigations=matching_investigations,
            )
            latest_log_at = _latest_log_observed_at(
                logs,
                project_id=binding.project_id,
                target_id=binding.registry_target_id,
                service_name=registration.compose_service,
            )
            projections.append(
                _ServiceInstanceProjection(
                    target=target,
                    binding=binding,
                    registration=registration,
                    status=status,
                    last_tested_at=latest_tests.get(target.target_id).observed_at
                    if target.target_id in latest_tests
                    else None,
                    last_observed_at=_max_datetime(
                        (
                            latest_tests.get(target.target_id).observed_at
                            if target.target_id in latest_tests
                            else None,
                            latest_log_at,
                            max(
                                (
                                    subscription.updated_at
                                    for subscription in subscriptions
                                ),
                                default=None,
                            ),
                            max(
                                (
                                    investigation.updated_at
                                    for investigation in matching_investigations
                                ),
                                default=None,
                            ),
                        )
                    ),
                    issue_ids=issue_ids,
                    investigation_ids=tuple(
                        investigation.investigation_id for investigation in matching_investigations
                    ),
                    pending_approval_ids=pending_approval_ids,
                    subscriptions=subscriptions,
                    records=recent_errors,
                )
            )
    projections.sort(key=lambda item: (item.registration.compose_service, item.target.target_id))
    return tuple(projections)


class ServiceProjectionService:
    def __init__(
        self,
        *,
        target_service: TargetService,
        target_store: TargetStore,
        projects: ProjectRegistryStore,
        approvals: ApprovalStore,
        investigations: InvestigationStore,
        operations: OperationStore,
        logs: LogStore,
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
        self._now = now or (lambda: datetime.now(UTC))
        self._windows = windows or ProjectionWindows()

    def read_service(
        self,
        service_id: str,
        *,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> ServiceDetailView | None:
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
            service_id=service_id,
        )
        if not instances:
            return None

        grouped_sources: dict[
            tuple[object, object], list[_ServiceInstanceProjection]
        ] = defaultdict(list)
        for instance in instances:
            for subscription in instance.subscriptions:
                grouped_sources[(subscription.source_kind, subscription.scope)].append(instance)

        log_sources = []
        for (source_kind, scope), source_instances in sorted(
            grouped_sources.items(),
            key=lambda item: (item[0][0].value, item[0][1].value),
        ):
            subscriptions = tuple(
                subscription
                for instance in source_instances
                for subscription in instance.subscriptions
                if subscription.source_kind is source_kind and subscription.scope is scope
            )
            latest_source_at = _max_datetime(
                tuple(
                    _latest_log_observed_at(
                        self._logs,
                        project_id=instance.binding.project_id,
                        target_id=instance.binding.registry_target_id,
                        service_name=instance.registration.compose_service,
                        source_kind=source_kind.value,
                        scope=scope.value,
                    )
                    for instance in source_instances
                )
            )
            log_sources.append(
                LogSourceSummary(
                    source_kind=source_kind,
                    scope=scope,
                    active_subscriptions=sum(
                        1
                        for subscription in subscriptions
                        if subscription.status is LogSubscriptionStatus.ACTIVE
                    ),
                    error_subscriptions=sum(
                        1
                        for subscription in subscriptions
                        if subscription.status is LogSubscriptionStatus.ERROR
                    ),
                    last_observed_at=latest_source_at,
                )
            )

        view_instances = tuple(
            ServiceInstanceView(
                target_id=instance.target.target_id,
                target_name=instance.target.name,
                host=instance.target.host,
                status=instance.status,
                container_names=instance.registration.container_names,
                issue_ids=instance.issue_ids,
                investigation_ids=instance.investigation_ids,
                pending_approval_count=len(instance.pending_approval_ids),
                last_tested_at=instance.last_tested_at,
                last_observed_at=instance.last_observed_at,
            )
            for instance in instances
        )
        return ServiceDetailView(
            generated_at=generated_at,
            service_id=service_id,
            status=_aggregate_status(tuple(instance.status for instance in instances)),
            target_ids=tuple(instance.target.target_id for instance in instances),
            issue_ids=tuple(
                dict.fromkeys(
                    issue_id
                    for instance in instances
                    for issue_id in instance.issue_ids
                )
            ),
            investigation_ids=tuple(
                dict.fromkeys(
                    investigation_id
                    for instance in instances
                    for investigation_id in instance.investigation_ids
                )
            ),
            pending_approval_count=len(
                {
                    approval_id
                    for instance in instances
                    for approval_id in instance.pending_approval_ids
                }
            ),
            instances=view_instances,
            log_sources=tuple(log_sources),
            last_observed_at=_max_datetime(
                tuple(instance.last_observed_at for instance in instances)
            ),
        )


__all__ = [
    "ProjectionWindows",
    "ServiceProjectionService",
    "build_service_instances",
    "_aggregate_status",
    "_issue_id",
]
