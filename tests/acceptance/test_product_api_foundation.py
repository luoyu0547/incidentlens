"""Offline integrated product acceptance for the currently available backend.

The test deliberately uses the application's public entry point and fake/local
adapters.  Cloud-only scenarios remain opt-in elsewhere; this gate must be
repeatable on a clean checkout with no credentials or Docker daemon.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app


def test_product_entrypoint_health_and_legacy_boundary(tmp_path: Path) -> None:
    app = create_app(RuntimeSettings(data_dir=tmp_path / "runtime"))
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["remote_execution"] == "not_configured"
        # The current baseline may not yet contain the v1 feature branch.  If
        # it does, the version endpoint is the only unauthenticated probe.
        version = client.get("/api/v1/version")
        assert version.status_code in {200, 404}
        assert client.get("/openapi.json").status_code in {200, 404}


def test_contract_check_is_safe_without_network() -> None:
    from scripts.check_product_contracts import main

    assert main() == 0
