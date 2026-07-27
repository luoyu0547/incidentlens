"""Tests for Docker Compose configuration — TDD RED phase.

Tests cover:
  - compose.yaml is valid YAML with expected services
  - All 4 services are defined (gateway, order, payment, control-plane)
  - Health checks are configured
  - SQLite volume is defined for control plane
"""

from __future__ import annotations

from pathlib import Path

import pytest

COMPOSE_PATH = Path(__file__).parent.parent.parent / "infra" / "compose" / "compose.yaml"


class TestComposeConfig:
    """Tests for Docker Compose configuration file."""

    def test_compose_file_exists(self) -> None:
        """compose.yaml should exist at the expected path."""
        assert COMPOSE_PATH.exists(), f"compose.yaml not found at {COMPOSE_PATH}"

    @pytest.mark.skipif(
        not COMPOSE_PATH.exists(), reason="compose.yaml not yet created"
    )
    def test_compose_has_four_services(self) -> None:
        """compose.yaml should define 4 services."""
        import yaml

        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        services = config.get("services", {})
        assert len(services) == 4

    @pytest.mark.skipif(
        not COMPOSE_PATH.exists(), reason="compose.yaml not yet created"
    )
    def test_compose_has_expected_service_names(self) -> None:
        """compose.yaml should have gateway, order, payment, control-plane services."""
        import yaml

        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        services = config.get("services", {})
        expected = {"gateway-service", "order-service", "payment-service", "control-plane"}
        assert set(services.keys()) == expected

    @pytest.mark.skipif(
        not COMPOSE_PATH.exists(), reason="compose.yaml not yet created"
    )
    def test_control_plane_has_sqlite_volume(self) -> None:
        """Control plane service should have a SQLite volume mounted."""
        import yaml

        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        cp = config["services"]["control-plane"]
        volumes = cp.get("volumes", [])
        has_data_volume = any("data" in str(v) for v in volumes)
        assert has_data_volume, "Control plane should have a data volume for SQLite"

    @pytest.mark.skipif(
        not COMPOSE_PATH.exists(), reason="compose.yaml not yet created"
    )
    def test_services_have_healthchecks(self) -> None:
        """All services should have health check configurations."""
        import yaml

        with open(COMPOSE_PATH) as f:
            config = yaml.safe_load(f)
        for name, svc in config["services"].items():
            assert "healthcheck" in svc, f"Service {name} missing healthcheck"
