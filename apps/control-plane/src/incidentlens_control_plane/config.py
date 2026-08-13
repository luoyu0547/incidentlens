"""Local runtime configuration and service construction."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    InvestigationBudget,
)


class RuntimeSettings(BaseModel):
    """Immutable settings for the local runtime.

    Every Phase 4 agent-runtime bound has a bounded default here so the
    runtime is never launched with an unbounded or arbitrary budget: the
    orchestrator's default run budget, the investigation service's default
    investigation budget, the global child cap and the active-investigation cap
    are all derived from these settings in ``build_runtime``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path
    max_active_log_subscriptions: int = Field(default=20, ge=1, le=1_000)
    log_subscription_queue_size: int = Field(default=1000, ge=1, le=100_000)
    log_subscription_batch_size: int = Field(default=100, ge=1, le=10_000)
    log_file_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0)

    # -- active runtime caps --------------------------------------------------
    # How many non-terminal investigations may exist at once and how many
    # container children may run concurrently across all investigations.
    max_active_investigations: int = Field(default=4, ge=1, le=64)
    max_active_children: int = Field(default=8, ge=1, le=64)

    # -- per-run default budget bounds ---------------------------------------
    # These mirror the ``AgentBudget`` contract bounds and become the default
    # budget for a run when a caller does not supply one.
    max_rounds_per_run: int = Field(default=8, ge=1, le=200)
    max_tool_calls_per_run: int = Field(default=16, ge=1, le=500)
    max_run_wall_clock_seconds: int = Field(default=1_800, ge=1, le=43_200)
    max_output_bytes_per_tool: int = Field(
        default=512 * 1024, ge=1, le=64 * 1024 * 1024
    )
    max_total_output_bytes_per_run: int = Field(
        default=4 * 1024 * 1024, ge=1, le=128 * 1024 * 1024
    )
    max_evidence_per_run: int = Field(default=100, ge=1, le=5_000)
    max_no_new_evidence_rounds: int = Field(default=3, ge=1, le=20)

    # -- per-investigation default budget bounds ------------------------------
    max_investigation_rounds: int = Field(default=32, ge=1, le=1_000)
    max_investigation_tool_calls: int = Field(default=64, ge=1, le=2_000)
    max_children_per_investigation: int = Field(default=4, ge=0, le=32)
    max_investigation_wall_clock_seconds: int = Field(
        default=7_200, ge=1, le=86_400
    )
    max_investigation_total_output_bytes: int = Field(
        default=16 * 1024 * 1024, ge=1, le=512 * 1024 * 1024
    )
    max_evidence_per_investigation: int = Field(default=300, ge=1, le=10_000)

    # -- shutdown -------------------------------------------------------------
    # How long the recovery service waits for active agent loops to observe a
    # cancellation request and drain before it force-cancels and sweeps.
    shutdown_grace_seconds: float = Field(default=10.0, ge=0.5, le=300.0)

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        """Create settings from the INCIDENTLENS_DATA_DIR environment variable."""
        configured = os.environ.get("INCIDENTLENS_DATA_DIR")
        data_dir = Path(configured).expanduser() if configured else Path.home() / ".incidentlens"
        return cls(data_dir=data_dir.resolve())

    def default_run_budget(self) -> AgentBudget:
        """The bounded default budget applied to a run without an explicit one."""
        return AgentBudget(
            max_rounds=self.max_rounds_per_run,
            max_tool_calls=self.max_tool_calls_per_run,
            max_wall_clock_seconds=self.max_run_wall_clock_seconds,
            max_output_bytes_per_tool=self.max_output_bytes_per_tool,
            max_total_output_bytes=self.max_total_output_bytes_per_run,
            max_evidence=self.max_evidence_per_run,
            max_no_new_evidence_rounds=self.max_no_new_evidence_rounds,
        )

    def default_investigation_budget(self) -> InvestigationBudget:
        """The bounded default budget for an investigation without an explicit one."""
        return InvestigationBudget(
            max_rounds=self.max_investigation_rounds,
            max_tool_calls=self.max_investigation_tool_calls,
            max_children=self.max_children_per_investigation,
            max_wall_clock_seconds=self.max_investigation_wall_clock_seconds,
            max_total_output_bytes=self.max_investigation_total_output_bytes,
            max_evidence=self.max_evidence_per_investigation,
            max_no_new_evidence_rounds=self.max_no_new_evidence_rounds,
        )
