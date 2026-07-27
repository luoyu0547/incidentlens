"""Tests for fault scenario lifecycle — enable, disable, reset.

These tests verify:
  - ScenarioService.enable() activates a fault with parameters
  - ScenarioService.disable() deactivates a specific fault
  - ScenarioService.reset() clears all active faults
  - ScenarioService.active_for() returns current faults for a service
  - Root cause labels are NOT exposed via any API
  - Faults actually change service behavior
"""

from __future__ import annotations

import os

import pytest

# Ensure ASGI transport is used for inter-service calls in tests
os.environ.pop("ORDER_SERVICE_URL", None)
os.environ.pop("PAYMENT_SERVICE_URL", None)


def _patch_service_urls() -> None:
    """Set service URL constants to empty so ASGI transport is used."""
    import order_service.main as ord_mod

    ord_mod.PAYMENT_SERVICE_URL = ""


_patch_service_urls()

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def service():
    """Create a ScenarioService instance."""
    from incidentlens_scenarios.service import ScenarioService

    return ScenarioService()


# ===================================================================
# LIFECYCLE TESTS
# ===================================================================


class TestScenarioLifecycle:
    """Tests for enable/disable/reset lifecycle."""

    def test_enable_payment_delay(self, service) -> None:
        """Enabling payment_delay stores the fault parameters."""
        service.enable("payment_delay", {"delay_ms": 250})
        active = service.active_for("payment-service")
        assert "payment_delay" in active
        assert active["payment_delay"]["delay_ms"] == 250

    def test_enable_payment_error_rate(self, service) -> None:
        """Enabling payment_error_rate stores the fault parameters."""
        service.enable("payment_error_rate", {"error_rate": 0.5})
        active = service.active_for("payment-service")
        assert "payment_error_rate" in active
        assert active["payment_error_rate"]["error_rate"] == 0.5

    def test_enable_db_pool_exhaustion(self, service) -> None:
        """Enabling db_pool_exhaustion stores the fault parameters."""
        service.enable("db_pool_exhaustion", {"pool_size": 1})
        active = service.active_for("order-service")
        assert "db_pool_exhaustion" in active

    def test_enable_dependency_unavailable(self, service) -> None:
        """Enabling dependency_unavailable stores the fault parameters."""
        service.enable("dependency_unavailable", {"dependency": "payment-service"})
        active = service.active_for("order-service")
        assert "dependency_unavailable" in active

    def test_enable_deployment_regression(self, service) -> None:
        """Enabling deployment_regression stores the fault parameters."""
        service.enable("deployment_regression", {"version": "v2.0.0-buggy"})
        active = service.active_for("payment-service")
        assert "deployment_regression" in active

    def test_disable_specific_fault(self, service) -> None:
        """Disabling a specific fault removes it while keeping others."""
        service.enable("payment_delay", {"delay_ms": 250})
        service.enable("payment_error_rate", {"error_rate": 0.5})
        service.disable("payment_delay")
        active = service.active_for("payment-service")
        assert "payment_delay" not in active
        assert "payment_error_rate" in active

    def test_delay_reset(self, service) -> None:
        """Reset clears all active faults for all services."""
        service.enable("payment_delay", {"delay_ms": 250})
        service.enable("db_pool_exhaustion", {"pool_size": 1})
        service.reset()
        assert service.active_for("payment-service") == {}
        assert service.active_for("order-service") == {}

    def test_active_for_returns_empty_when_no_faults(self, service) -> None:
        """active_for returns empty dict when no faults are active."""
        assert service.active_for("payment-service") == {}

    def test_enable_unknown_scenario_raises(self, service) -> None:
        """Enabling an unknown scenario name raises ValueError."""
        with pytest.raises(ValueError, match="unknown scenario"):
            service.enable("nonexistent_fault", {})


# ===================================================================
# ROOT CAUSE LABEL ISOLATION TESTS
# ===================================================================


class TestRootCauseIsolation:
    """Root cause labels must NOT be exposed via any API."""

    def test_active_for_does_not_expose_root_cause(self, service) -> None:
        """active_for must not contain root_cause_label in its output."""
        service.enable("payment_delay", {"delay_ms": 250})
        active = service.active_for("payment-service")
        for fault_name, params in active.items():
            assert "root_cause" not in str(params).lower(), (
                f"Root cause label leaked in {fault_name}: {params}"
            )

    def test_scenario_models_have_root_cause_internally(self, service) -> None:
        """Scenario definitions store root_cause_label internally but it's not in API output."""
        from incidentlens_scenarios.models import SCENARIOS

        for name, scenario in SCENARIOS.items():
            assert "root_cause_label" in scenario, (
                f"Scenario {name} must have root_cause_label internally"
            )
        # But active_for must not expose it
        service.enable("payment_delay", {"delay_ms": 250})
        active = service.active_for("payment-service")
        for params in active.values():
            assert "root_cause_label" not in params

    def test_get_params_does_not_expose_root_cause(self, service) -> None:
        """get_params must not contain root_cause_label in its output."""
        service.enable("payment_delay", {"delay_ms": 250})
        params = service.get_params("payment_delay")
        assert params is not None
        assert "root_cause_label" not in params
        assert params["delay_ms"] == 250


# ===================================================================
# FAULT BEHAVIOR TESTS
# ===================================================================


class TestFaultBehavior:
    """Faults must actually change service behavior."""

    @pytest.mark.asyncio()
    async def test_payment_delay_adds_real_delay(self) -> None:
        """When payment_delay is enabled, payment service adds real delay."""
        import time

        from httpx import ASGITransport, AsyncClient
        from incidentlens_scenarios.service import ScenarioService
        from payment_service.main import app, set_scenario_service

        svc = ScenarioService()
        svc.enable("payment_delay", {"delay_ms": 100})
        set_scenario_service(svc)

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://payment")
        start = time.monotonic()
        response = await client.post(
            "/charge",
            json={"amount": 100, "currency": "USD"},
            headers={"X-Trace-ID": "trace-delay", "X-Request-ID": "req-delay"},
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        assert response.status_code == 200
        assert elapsed_ms >= 90  # Allow small timing variance

        # Reset
        svc.reset()
        set_scenario_service(None)

    @pytest.mark.asyncio()
    async def test_payment_error_rate_returns_500(self) -> None:
        """When payment_error_rate is enabled at 1.0, payment service returns 500."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_scenarios.service import ScenarioService
        from payment_service.main import app, set_scenario_service

        svc = ScenarioService()
        svc.enable("payment_error_rate", {"error_rate": 1.0})
        set_scenario_service(svc)

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://payment")
        response = await client.post(
            "/charge",
            json={"amount": 100, "currency": "USD"},
            headers={"X-Trace-ID": "trace-err", "X-Request-ID": "req-err"},
        )
        assert response.status_code == 500

        # Reset
        svc.reset()
        set_scenario_service(None)

    @pytest.mark.asyncio()
    async def test_dependency_unavailable_returns_502(self) -> None:
        """When dependency_unavailable is enabled, order service returns 502."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_scenarios.service import ScenarioService
        from order_service.main import app, set_scenario_service

        svc = ScenarioService()
        svc.enable("dependency_unavailable", {"dependency": "payment-service"})
        set_scenario_service(svc)

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://order")
        response = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 1},
            headers={"X-Trace-ID": "trace-dep", "X-Request-ID": "req-dep"},
        )
        assert response.status_code == 502

        # Reset
        svc.reset()
        set_scenario_service(None)

    @pytest.mark.asyncio()
    async def test_db_pool_exhaustion_causes_delay(self) -> None:
        """When db_pool_exhaustion is enabled, order service adds delay based on pool_size."""
        import time

        from httpx import ASGITransport, AsyncClient
        from incidentlens_scenarios.service import ScenarioService
        from order_service.main import app, set_scenario_service

        svc = ScenarioService()
        # pool_size=1 => delay = max(0.1, 1.0/1) = 1.0s; use pool_size=4 for faster test
        svc.enable("db_pool_exhaustion", {"pool_size": 4})
        set_scenario_service(svc)

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://order")
        start = time.monotonic()
        response = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 1},
            headers={"X-Trace-ID": "trace-pool", "X-Request-ID": "req-pool"},
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        # pool_size=4 => delay = max(0.1, 1.0/4) = 0.25s = 250ms
        assert response.status_code == 201
        assert elapsed_ms >= 200  # Allow small timing variance

        # Reset
        svc.reset()
        set_scenario_service(None)

    @pytest.mark.asyncio()
    async def test_deployment_regression_returns_zero_amount(self) -> None:
        """When deployment_regression is enabled, payment service returns amount=0."""
        from httpx import ASGITransport, AsyncClient
        from incidentlens_scenarios.service import ScenarioService
        from payment_service.main import app, set_scenario_service

        svc = ScenarioService()
        svc.enable("deployment_regression", {"version": "v2.0.0-buggy"})
        set_scenario_service(svc)

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://payment")
        response = await client.post(
            "/charge",
            json={"amount": 500, "currency": "USD"},
            headers={"X-Trace-ID": "trace-regression", "X-Request-ID": "req-regression"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["amount"] == 0  # Buggy deployment returns zero amount

        # Reset
        svc.reset()
        set_scenario_service(None)
