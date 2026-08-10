from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TargetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    ssh_user: str = Field(min_length=1, max_length=80)
    ssh_config_alias: str | None = Field(default=None, min_length=1, max_length=255)


class ServiceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    compose_service: str = Field(min_length=1, max_length=120)
    container_names: tuple[str, ...] = ()
    local_source_path: Path | None = None
    container_path_hints: tuple[str, ...] = ()
    allowed_log_paths: tuple[str, ...] = ()

    @field_validator("local_source_path")
    @classmethod
    def local_source_path_must_be_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("local_source_path must be absolute")
        return value


class ProjectRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    display_name: str = Field(min_length=1, max_length=120)
    local_source_paths: tuple[Path, ...] = ()
    targets: tuple[TargetRegistration, ...] = ()
    services: tuple[ServiceRegistration, ...] = ()

    @field_validator("local_source_paths")
    @classmethod
    def local_paths_must_be_absolute(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(not value.is_absolute() for value in values):
            raise ValueError("local_source_paths must be absolute")
        return values

    @model_validator(mode="after")
    def associations_must_be_unique(self) -> "ProjectRegistration":
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("target_id values must be unique")
        service_names = [service.compose_service for service in self.services]
        if len(service_names) != len(set(service_names)):
            raise ValueError("compose_service values must be unique")
        return self


class ProjectRecord(ProjectRegistration):
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_registration(
        cls, registration: ProjectRegistration, *, created_at: datetime
    ) -> "ProjectRecord":
        return cls(
            **registration.model_dump(),
            created_at=created_at,
            updated_at=created_at,
        )

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value
