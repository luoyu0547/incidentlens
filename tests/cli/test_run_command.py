"""Tests for the one-command investigation launch request."""

from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.cli.app import _parse_args
from incidentlens_control_plane.cli.run_request import RunRequest, RunRequestError
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)


def registry(tmp_path) -> ProjectRegistryStore:
    import sqlite3

    store = ProjectRegistryStore(lambda: sqlite3.connect(tmp_path / "registry.db"))
    store.migrate()
    store.create(
        ProjectRegistration(
            project_id="cloud",
            display_name="Cloud acceptance",
            targets=(
                TargetRegistration(
                    target_id="vm",
                    host="private.example",
                    ssh_user="operator",
                    ssh_config_alias="acceptance-vm",
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service="api-gateway",
                    container_names=("gateway-1",),
                    allowed_host_paths=(PurePosixPath("/opt/target"),),
                    allowed_container_paths=(PurePosixPath("/app"),),
                ),
            ),
        ),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    return store


def test_host_scope_is_derived_only_from_registration(tmp_path) -> None:
    scope = RunRequest(
        project_id="cloud",
        target_id="vm",
        service="api-gateway",
        scope=LogScope.HOST,
        symptom="orders fail",
    ).resolve_scope(registry(tmp_path))
    assert scope.allowed_host_paths == (PurePosixPath("/opt/target"),)
    assert scope.service_name is None


def test_unknown_target_is_rejected(tmp_path) -> None:
    request = RunRequest(
        project_id="cloud",
        target_id="missing",
        service="api-gateway",
        scope=LogScope.HOST,
        symptom="orders fail",
    )
    with pytest.raises(RunRequestError, match="not registered"):
        request.resolve_scope(registry(tmp_path))


def test_run_parser_accepts_exact_one_command(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "incidentlens",
            "run",
            "--project",
            "cloud",
            "--target",
            "vm",
            "--service",
            "api-gateway",
            "--scope",
            "host",
            "--record",
            "session.cast",
            "orders fail",
        ],
    )
    args = _parse_args()
    assert args.command == "run"
    assert args.project_id == "cloud"
    assert args.record == "session.cast"
    assert not hasattr(args, "host")
    assert not hasattr(args, "api_key")
