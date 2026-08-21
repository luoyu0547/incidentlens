"""Validated one-command investigation launch request."""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.investigation.types import AgentScope
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore


class RunRequestError(ValueError):
    """Raised when a CLI run request cannot be derived from registration."""


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=120)
    scope: LogScope
    symptom: str = Field(min_length=1, max_length=2_000)
    record: str | None = None

    def resolve_scope(self, projects: ProjectRegistryStore) -> AgentScope:
        project = projects.get(self.project_id)
        if not any(target.target_id == self.target_id for target in project.targets):
            raise RunRequestError(
                f"target {self.target_id!r} is not registered for {self.project_id!r}"
            )
        service = next(
            (item for item in project.services if item.compose_service == self.service),
            None,
        )
        if service is None:
            raise RunRequestError(
                f"service {self.service!r} is not registered for {self.project_id!r}"
            )
        if self.scope is LogScope.HOST:
            return AgentScope(
                project_id=self.project_id,
                target_id=self.target_id,
                scope=LogScope.HOST,
                allowed_host_paths=tuple(
                    PurePosixPath(path) for path in service.allowed_host_paths
                ),
            )
        if len(service.container_names) != 1:
            raise RunRequestError(
                "container scope requires exactly one registered container"
            )
        return AgentScope(
            project_id=self.project_id,
            target_id=self.target_id,
            scope=LogScope.CONTAINER,
            service_name=self.service,
            container_name=service.container_names[0],
            allowed_container_paths=tuple(
                PurePosixPath(path) for path in service.allowed_container_paths
            ),
        )


def add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run")
    parser.add_argument("--project", required=True, dest="project_id")
    parser.add_argument("--target", required=True, dest="target_id")
    parser.add_argument("--service", required=True)
    parser.add_argument("--scope", choices=[item.value for item in LogScope], required=True)
    parser.add_argument("--record")
    parser.add_argument("symptom")


def request_from_args(args: argparse.Namespace) -> RunRequest:
    return RunRequest(
        project_id=args.project_id,
        target_id=args.target_id,
        service=args.service,
        scope=LogScope(args.scope),
        symptom=args.symptom,
        record=args.record,
    )


__all__ = ["RunRequest", "RunRequestError", "add_run_parser", "request_from_args"]
