"""ScenarioService — manage fault scenario lifecycle.

Public interface:
  - enable(name, params)  — activate a fault scenario with parameters
  - disable(name)         — deactivate a specific fault scenario
  - reset()               — clear all active faults
  - active_for(service)   — return active faults for a given service
  - is_active(name)       — check if a specific scenario is active
  - get_params(name)      — get parameters for an active scenario
"""

from __future__ import annotations

from typing import Any

from incidentlens_scenarios.models import SCENARIOS


class ScenarioService:
    """Manage fault scenario lifecycle: enable, disable, reset, query."""

    def __init__(self) -> None:
        # _active maps scenario_name -> params (without root_cause_label)
        self._active: dict[str, dict[str, Any]] = {}

    def enable(self, name: str, params: dict[str, Any] | None = None) -> None:
        """Activate a fault scenario with the given parameters.

        Raises ValueError if the scenario name is unknown.
        """
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario: {name}")
        merged = dict(SCENARIOS[name]["default_params"])
        if params:
            merged.update(params)
        self._active[name] = merged

    def disable(self, name: str) -> None:
        """Deactivate a specific fault scenario."""
        self._active.pop(name, None)

    def reset(self) -> None:
        """Clear all active faults."""
        self._active.clear()

    def active_for(self, service: str) -> dict[str, dict[str, Any]]:
        """Return active faults for a given service.

        The returned dict maps scenario_name -> params.
        Root cause labels are NOT included in the output.
        """
        result: dict[str, dict[str, Any]] = {}
        for name, params in self._active.items():
            scenario_def = SCENARIOS.get(name)
            if scenario_def and scenario_def["target_service"] == service:
                # Explicitly exclude root_cause_label from output
                safe_params = {k: v for k, v in params.items() if k != "root_cause_label"}
                result[name] = safe_params
        return result

    def is_active(self, name: str) -> bool:
        """Check if a specific scenario is currently active."""
        return name in self._active

    def get_params(self, name: str) -> dict[str, Any] | None:
        """Get parameters for an active scenario, or None if not active."""
        return self._active.get(name)
