"""Tests for ScenarioStore — persistent scenario state via SQLAlchemy.

These tests verify:
  - ScenarioStore persists scenario state to SQLite via SQLAlchemy
  - enable(name, params) activates a scenario and persists it
  - disable(name) deactivates a scenario and persists the removal
  - reset() clears all active scenarios
  - runtime_for(service) returns only safe parameters (never root_cause_label)
  - A new ScenarioStore instance reads persisted state from the same engine
  - Unknown scenario names raise ValueError
  - Parameter validation rejects invalid ranges
"""

from __future__ import annotations

import pytest
from incidentlens_telemetry.database import create_engine
from sqlalchemy import Engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """Create an in-memory SQLite engine with all tables."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture()
def store(engine: Engine):
    """Create a ScenarioStore instance backed by the test engine."""
    from incidentlens_scenarios.store import ScenarioStore

    return ScenarioStore(engine)


# ===================================================================
# PERSISTENCE TESTS
# ===================================================================


class TestScenarioStorePersistence:
    """Tests for scenario state persistence across store instances."""

    def test_enable_persists_across_instances(self, engine: Engine) -> None:
        """Enabling a scenario persists it so a new store instance sees it."""
        from incidentlens_scenarios.store import ScenarioStore

        store1 = ScenarioStore(engine)
        store1.enable("payment_delay", {"delay_ms": 250})

        # New instance on same engine should see the persisted state
        store2 = ScenarioStore(engine)
        active = store2.runtime_for("payment-service")
        assert "payment_delay" in active
        assert active["payment_delay"]["delay_ms"] == 250

    def test_disable_persists_across_instances(self, engine: Engine) -> None:
        """Disabling a scenario persists the removal."""
        from incidentlens_scenarios.store import ScenarioStore

        store1 = ScenarioStore(engine)
        store1.enable("payment_delay", {"delay_ms": 250})
        store1.disable("payment_delay")

        store2 = ScenarioStore(engine)
        active = store2.runtime_for("payment-service")
        assert "payment_delay" not in active

    def test_reset_persists_across_instances(self, engine: Engine) -> None:
        """Reset clears all scenarios and persists the empty state."""
        from incidentlens_scenarios.store import ScenarioStore

        store1 = ScenarioStore(engine)
        store1.enable("payment_delay", {"delay_ms": 250})
        store1.enable("db_pool_exhaustion", {"pool_size": 1})
        store1.reset()

        store2 = ScenarioStore(engine)
        assert store2.runtime_for("payment-service") == {}
        assert store2.runtime_for("order-service") == {}


# ===================================================================
# RUNTIME PROJECTION TESTS
# ===================================================================


class TestRuntimeProjection:
    """Tests for runtime_for() — the safe projection used by tools and API."""

    def test_runtime_projection_excludes_internal_label(self, store) -> None:
        """runtime_for must never return root_cause_label."""
        store.enable("payment_delay", {"delay_ms": 250})
        runtime = store.runtime_for("payment-service")
        assert "payment_delay" in runtime
        assert "root_cause_label" not in runtime["payment_delay"]
        # Also check repr to ensure no leakage in string representation
        assert "root_cause_label" not in repr(runtime)

    def test_runtime_for_filters_by_service(self, store) -> None:
        """runtime_for only returns scenarios targeting the given service."""
        store.enable("payment_delay", {"delay_ms": 250})
        store.enable("db_pool_exhaustion", {"pool_size": 1})

        payment_runtime = store.runtime_for("payment-service")
        order_runtime = store.runtime_for("order-service")

        assert "payment_delay" in payment_runtime
        assert "db_pool_exhaustion" not in payment_runtime
        assert "db_pool_exhaustion" in order_runtime
        assert "payment_delay" not in order_runtime

    def test_runtime_for_returns_empty_for_unknown_service(self, store) -> None:
        """runtime_for returns empty dict for a service with no active scenarios."""
        assert store.runtime_for("unknown-service") == {}

    def test_runtime_for_returns_empty_when_no_scenarios_active(self, store) -> None:
        """runtime_for returns empty dict when no scenarios are active."""
        assert store.runtime_for("payment-service") == {}

    def test_runtime_for_all_scenarios_exclude_root_cause(self, store) -> None:
        """All scenario types must exclude root_cause_label from runtime_for."""
        store.enable("payment_delay", {"delay_ms": 250})
        store.enable("payment_error_rate", {"error_rate": 0.5})
        store.enable("db_pool_exhaustion", {"pool_size": 1})
        store.enable("dependency_unavailable", {"dependency": "payment-service"})
        store.enable("deployment_regression", {"version": "v2.0.0-buggy"})

        for service in ("payment-service", "order-service"):
            runtime = store.runtime_for(service)
            for name, params in runtime.items():
                assert "root_cause_label" not in params, (
                    f"root_cause_label leaked in {name}: {params}"
                )


# ===================================================================
# ENABLE / DISABLE / RESET TESTS
# ===================================================================


class TestScenarioStoreLifecycle:
    """Tests for enable, disable, and reset operations."""

    def test_enable_merges_default_params(self, store) -> None:
        """Enable merges user params with default params."""
        store.enable("payment_delay", {"delay_ms": 500})
        runtime = store.runtime_for("payment-service")
        assert runtime["payment_delay"]["delay_ms"] == 500

    def test_enable_uses_defaults_when_no_params(self, store) -> None:
        """Enable uses default params when none are provided."""
        store.enable("payment_delay")
        runtime = store.runtime_for("payment-service")
        assert runtime["payment_delay"]["delay_ms"] == 200  # default

    def test_enable_unknown_scenario_raises(self, store) -> None:
        """Enabling an unknown scenario name raises ValueError."""
        with pytest.raises(ValueError, match="unknown scenario"):
            store.enable("nonexistent_fault", {})

    def test_disable_nonexistent_is_noop(self, store) -> None:
        """Disabling a scenario that is not active is a no-op."""
        store.disable("payment_delay")  # Should not raise

    def test_reset_clears_all(self, store) -> None:
        """Reset clears all active scenarios."""
        store.enable("payment_delay", {"delay_ms": 250})
        store.enable("payment_error_rate", {"error_rate": 0.5})
        store.enable("db_pool_exhaustion", {"pool_size": 1})
        store.reset()
        assert store.runtime_for("payment-service") == {}
        assert store.runtime_for("order-service") == {}

    def test_enable_replaces_existing_params(self, store) -> None:
        """Re-enabling a scenario replaces its parameters."""
        store.enable("payment_delay", {"delay_ms": 250})
        store.enable("payment_delay", {"delay_ms": 500})
        runtime = store.runtime_for("payment-service")
        assert runtime["payment_delay"]["delay_ms"] == 500


# ===================================================================
# PARAMETER VALIDATION TESTS
# ===================================================================


class TestParameterValidation:
    """Tests for parameter validation on enable."""

    def test_error_rate_must_be_between_0_and_1(self, store) -> None:
        """error_rate must be in [0, 1] range."""
        with pytest.raises(ValueError, match="error_rate"):
            store.enable("payment_error_rate", {"error_rate": 1.5})
        with pytest.raises(ValueError, match="error_rate"):
            store.enable("payment_error_rate", {"error_rate": -0.1})

    def test_delay_ms_must_be_positive(self, store) -> None:
        """delay_ms must be a positive number."""
        with pytest.raises(ValueError, match="delay_ms"):
            store.enable("payment_delay", {"delay_ms": -100})

    def test_pool_size_must_be_positive(self, store) -> None:
        """pool_size must be a positive integer."""
        with pytest.raises(ValueError, match="pool_size"):
            store.enable("db_pool_exhaustion", {"pool_size": 0})
        with pytest.raises(ValueError, match="pool_size"):
            store.enable("db_pool_exhaustion", {"pool_size": -1})

    def test_valid_params_are_accepted(self, store) -> None:
        """Valid parameter ranges are accepted without error."""
        store.enable("payment_error_rate", {"error_rate": 0.0})
        store.enable("payment_error_rate", {"error_rate": 1.0})
        store.enable("payment_delay", {"delay_ms": 1})
        store.enable("db_pool_exhaustion", {"pool_size": 1})
