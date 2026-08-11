"""C1 regression: the production runtime resolves registered targets end-to-end."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient


def test_remote_tools_read_resolves_registered_target(
    client: TestClient, runtime, tmp_path: Path
) -> None:
    """A registered target is resolved from the project record, not a hardcoded dict.

    Before the fix the built runtime passed ``targets={}`` to the gateway, so
    every typed remote operation raised ``ValueError: target '...' is not
    registered``.
    """
    response = client.post(
        "/api/projects",
        json={
            "project_id": "c1proj",
            "display_name": "C1 Project",
            "local_source_paths": [str((tmp_path / "src").resolve())],
            "targets": [
                {
                    "target_id": "c1-host",
                    "host": "c1.example.test",
                    "ssh_user": "deploy",
                }
            ],
            "services": [
                {
                    "compose_service": "c1-service",
                    "container_names": ["c1-1"],
                    "allowed_host_paths": ["/opt/payments"],
                }
            ],
        },
    )
    assert response.status_code == 201

    result = asyncio.run(
        runtime.remote_tools.read(
            project_id="c1proj",
            target_id="c1-host",
            service="c1-service",
            path=PurePosixPath("/opt/payments/app.py"),
        )
    )
    assert result.path == PurePosixPath("/opt/payments/app.py")
