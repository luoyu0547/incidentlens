"""Stable read-model contracts for overview and service projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.logs.types import LogScope, LogSourceKind


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProjectionWindows:
    target_test_lookback: timedelta = timedelta(minutes=30)
    subscription_lookback: timedelta = timedelta(minutes=30)
    error_lookback: timedelta = timedelta(minutes=30)
    resolution_lookback: timedelta = timedelta(days=7)
    max_recent_resolutions: int = 5


class StatusCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    healthy: int = Field(default=0, ge=0)
    degraded: int = Field(default=0, ge=0)
    unreachable: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)


class OverviewServiceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_id: str
    status: HealthStatus
    container_count: int = Field(ge=0)
    open_issue_count: int = Field(ge=0)
    pending_approval_count: int = Field(ge=0)
    last_observed_at: datetime | None = None


class OverviewTargetView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    name: str
    host: str
    status: HealthStatus
    service_count: int = Field(ge=0)
    services: tuple[OverviewServiceView, ...] = ()
    last_tested_at: datetime | None = None
    last_observed_at: datetime | None = None


class ResolutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    issue_id: str
    target_id: str
    service_id: str
    symptom: str
    resolution_summary: str
    verification_summary: str | None = None
    resolved_at: datetime


class OverviewView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    targets: tuple[OverviewTargetView, ...]
    service_counts: StatusCounts
    open_issue_count: int = Field(ge=0)
    active_investigation_count: int = Field(ge=0)
    pending_approval_count: int = Field(ge=0)
    recent_resolutions: tuple[ResolutionSummary, ...]


class LogSourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: LogSourceKind
    scope: LogScope
    active_subscriptions: int = Field(ge=0)
    error_subscriptions: int = Field(ge=0)
    last_observed_at: datetime | None = None


class ServiceInstanceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    target_name: str
    host: str
    status: HealthStatus
    container_names: tuple[str, ...]
    issue_ids: tuple[str, ...]
    investigation_ids: tuple[str, ...]
    pending_approval_count: int = Field(ge=0)
    last_tested_at: datetime | None = None
    last_observed_at: datetime | None = None


class ServiceDetailView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    service_id: str
    status: HealthStatus
    target_ids: tuple[str, ...]
    issue_ids: tuple[str, ...]
    investigation_ids: tuple[str, ...]
    pending_approval_count: int = Field(ge=0)
    instances: tuple[ServiceInstanceView, ...]
    log_sources: tuple[LogSourceSummary, ...]
    last_observed_at: datetime | None = None
