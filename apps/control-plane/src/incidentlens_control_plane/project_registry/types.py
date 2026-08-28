from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TargetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    ssh_user: str = Field(min_length=1, max_length=80)
    ssh_config_alias: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    compose_working_directory: PurePosixPath | None = None
    compose_project_name: str | None = Field(default=None, min_length=1, max_length=120)
    compose_files: tuple[PurePosixPath, ...] = ()
    validation_base_url: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("compose_working_directory")
    @classmethod
    def compose_working_directory_must_be_absolute(
        cls, value: PurePosixPath | None
    ) -> PurePosixPath | None:
        if value is not None:
            if not value.is_absolute():
                raise ValueError("compose_working_directory must be absolute")
            if ".." in value.parts:
                raise ValueError("compose_working_directory must not contain '..'")
        return value

    @model_validator(mode="after")
    def compose_files_must_be_scoped_to_project_directory(self) -> "TargetRegistration":
        if self.compose_files and self.compose_working_directory is None:
            raise ValueError("compose_files require compose_working_directory")
        for path in self.compose_files:
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("compose_files must be absolute")
            if not path.is_relative_to(self.compose_working_directory):
                raise ValueError("compose_files must be inside compose_working_directory")
        return self

    @field_validator("validation_base_url")
    @classmethod
    def validation_base_url_must_be_loopback_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("validation_base_url must be a valid URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("validation_base_url must be a loopback HTTP base URL")
        return value.rstrip("/")


class ServiceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    compose_service: str = Field(min_length=1, max_length=120)
    container_names: tuple[str, ...] = ()
    local_source_path: Path | None = None
    container_path_hints: tuple[str, ...] = ()
    allowed_log_paths: tuple[str, ...] = ()
    allowed_host_paths: tuple[PurePosixPath, ...] = ()
    allowed_container_paths: tuple[PurePosixPath, ...] = ()
    protected_remote_paths: tuple[PurePosixPath, ...] = ()
    allowed_validation_scripts: tuple[PurePosixPath, ...] = ()

    @field_validator("local_source_path")
    @classmethod
    def local_source_path_must_be_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("local_source_path must be absolute")
        return value

    @field_validator(
        "allowed_host_paths",
        "allowed_container_paths",
        "protected_remote_paths",
        "allowed_validation_scripts",
    )
    @classmethod
    def remote_paths_must_be_absolute(
        cls, values: tuple[PurePosixPath, ...]
    ) -> tuple[PurePosixPath, ...]:
        for value in values:
            if not value.is_absolute():
                raise ValueError("absolute")
            if ".." in value.parts:
                raise ValueError("remote paths must not contain '..'")
        return values

    @model_validator(mode="after")
    def validation_scripts_must_be_inside_host_roots(self) -> "ServiceRegistration":
        for script in self.allowed_validation_scripts:
            if not any(script.is_relative_to(root) for root in self.allowed_host_paths):
                raise ValueError("allowed_validation_scripts must be inside allowed_host_paths")
        return self


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
