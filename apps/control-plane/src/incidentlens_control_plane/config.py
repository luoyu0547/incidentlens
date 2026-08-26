"""Local runtime configuration and service construction."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    InvestigationBudget,
)

#: Clearly-marked development signing key used when
#: ``INCIDENTLENS_SESSION_SIGNING_KEY`` is not set.  Never use this in
#: production: session forgery protection depends on a secret key.
DEFAULT_SESSION_SIGNING_KEY = "incidentlens-dev-only-session-signing-key-change-me"


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
    web_root: Path | None = None
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
    max_rounds_per_run: int = Field(default=20, ge=1, le=200)
    max_tool_calls_per_run: int = Field(default=40, ge=1, le=500)
    max_run_wall_clock_seconds: int = Field(default=1_800, ge=1, le=43_200)
    max_output_bytes_per_tool: int = Field(
        default=512 * 1024, ge=1, le=64 * 1024 * 1024
    )
    max_total_output_bytes_per_run: int = Field(
        default=4 * 1024 * 1024, ge=1, le=128 * 1024 * 1024
    )
    max_evidence_per_run: int = Field(default=100, ge=1, le=5_000)
    max_no_new_evidence_rounds: int = Field(default=3, ge=1, le=20)
    max_provider_retries: int = Field(default=2, ge=0, le=10)

    # -- active context materialization ----------------------------------------
    # The token-based budget that bounds every provider turn.
    agent_context_window_tokens: int = Field(default=128_000, ge=8_000, le=2_000_000)
    agent_context_max_output_tokens: int = Field(default=8_000, ge=256, le=128_000)
    agent_context_reserve_tokens: int = Field(default=13_000, ge=1_000, le=128_000)
    agent_tool_result_budget_chars: int = Field(
        default=200_000, ge=10_000, le=5_000_000
    )
    # Production-style time-based micro-compaction.  It is not a positional
    # "keep N recent tool results" rule.
    agent_micro_compact_after_seconds: int = Field(default=3_600, ge=60, le=86_400)
    # Fraction of ``max_input_tokens`` at which the agent loop asks the semantic
    # compactor to run, before the deterministic over-budget path is forced.
    # Stays below 1.0 so pressure compaction triggers before the hard ceiling.
    agent_context_semantic_compact_at_fraction: float = Field(default=0.9, gt=0, le=1)
    agent_compact_max_failures: int = Field(default=3, ge=1, le=10)
    agent_reactive_keep_recent_groups: int = Field(default=5, ge=1, le=20)

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

    # -- reports ---------------------------------------------------------------
    report_output_dir: Path | None = None
    agent_mode: str = "fake"
    llm_active_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None

    # -- API surface -------------------------------------------------------------
    # When false, the interactive docs (/docs, /redoc) and the served
    # /openapi.json are disabled; ``application.openapi()`` still works for
    # offline export.
    expose_api_docs: bool = False

    # -- authentication ---------------------------------------------------------
    # Static deployment profiles (SHA-256 token digests) and the signed-session
    # configuration.  ``auth_profiles_json`` and ``session_signing_key`` default
    # to empty / non-secret so a test or local runtime boots without ceremony,
    # and every legacy ``/api/*`` route stays open behind ``legacy_api_enabled``.
    auth_profiles_json: str | None = None
    session_signing_key: SecretStr = Field(
        default_factory=lambda: SecretStr(DEFAULT_SESSION_SIGNING_KEY)
    )
    session_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400)
    #: Allowed Host headers.  ``testserver`` keeps Starlette's TestClient (which
    #: sends host ``testserver``) working; ``localhost``/``127.0.0.1`` cover
    #: local browser and loopback health checks.
    trusted_hosts: list[str] = Field(
        default_factory=lambda: ["testserver", "localhost", "127.0.0.1"]
    )
    secure_cookies: bool = True
    legacy_api_enabled: bool = True

    # -- product streams -------------------------------------------------------
    # Bounded outbound queue for a versioned stream subscriber before the broker
    # treats it as a slow consumer and the stream closes the connection.
    stream_subscriber_queue_size: int = Field(default=256, ge=16, le=4096)
    stream_heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=120.0)

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        """Create local runtime settings from ``INCIDENTLENS_*`` environment variables."""
        _load_local_dotenv()
        configured = os.environ.get("INCIDENTLENS_DATA_DIR")
        data_dir = Path(configured).expanduser() if configured else Path.home() / ".incidentlens"
        report_output_dir = data_dir.resolve() / "reports"
        web_root = Path(__file__).resolve().parent / "static" / "web"
        session_key_value = os.environ.get("INCIDENTLENS_SESSION_SIGNING_KEY")
        session_signing_key = (
            SecretStr(session_key_value)
            if session_key_value
            else SecretStr(DEFAULT_SESSION_SIGNING_KEY)
        )
        return cls(
            data_dir=data_dir.resolve(),
            report_output_dir=report_output_dir,
            web_root=web_root,
            agent_mode=os.environ.get("INCIDENTLENS_AGENT_MODE", "fake"),
            llm_active_model=os.environ.get("INCIDENTLENS_LLM_ACTIVE_MODEL"),
            llm_base_url=os.environ.get("INCIDENTLENS_LLM_BASE_URL"),
            llm_api_key=os.environ.get("INCIDENTLENS_LLM_API_KEY"),
            expose_api_docs=_environment_bool(
                "INCIDENTLENS_EXPOSE_API_DOCS", False
            ),
            auth_profiles_json=os.environ.get("INCIDENTLENS_AUTH_PROFILES_JSON"),
            session_signing_key=session_signing_key,
            session_ttl_seconds=_environment_int(
                "INCIDENTLENS_SESSION_TTL_SECONDS", 3_600
            ),
            trusted_hosts=_environment_list(
                "INCIDENTLENS_TRUSTED_HOSTS", ["testserver", "localhost", "127.0.0.1"]
            ),
            secure_cookies=_environment_bool("INCIDENTLENS_SECURE_COOKIES", True),
            legacy_api_enabled=_environment_bool("INCIDENTLENS_LEGACY_API_ENABLED", True),
            agent_context_window_tokens=_environment_int(
                "INCIDENTLENS_AGENT_CONTEXT_WINDOW_TOKENS", 128_000
            ),
            agent_context_max_output_tokens=_environment_int(
                "INCIDENTLENS_AGENT_CONTEXT_MAX_OUTPUT_TOKENS", 8_000
            ),
            agent_context_reserve_tokens=_environment_int(
                "INCIDENTLENS_AGENT_CONTEXT_RESERVE_TOKENS", 13_000
            ),
            agent_tool_result_budget_chars=_environment_int(
                "INCIDENTLENS_AGENT_TOOL_RESULT_BUDGET_CHARS", 200_000
            ),
            agent_micro_compact_after_seconds=_environment_int(
                "INCIDENTLENS_AGENT_MICRO_COMPACT_AFTER_SECONDS", 3_600
            ),
            agent_context_semantic_compact_at_fraction=_environment_float(
                "INCIDENTLENS_AGENT_CONTEXT_SEMANTIC_COMPACT_AT_FRACTION", 0.9
            ),
            max_rounds_per_run=_environment_int("INCIDENTLENS_MAX_ROUNDS_PER_RUN", 20),
            max_tool_calls_per_run=_environment_int(
                "INCIDENTLENS_MAX_TOOL_CALLS_PER_RUN", 40
            ),
            max_no_new_evidence_rounds=_environment_int(
                "INCIDENTLENS_MAX_NO_NEW_EVIDENCE_ROUNDS", 3
            ),
            max_provider_retries=_environment_int("INCIDENTLENS_MAX_PROVIDER_RETRIES", 2),
            max_investigation_rounds=_environment_int(
                "INCIDENTLENS_MAX_INVESTIGATION_ROUNDS", 32
            ),
            max_investigation_tool_calls=_environment_int(
                "INCIDENTLENS_MAX_INVESTIGATION_TOOL_CALLS", 64
            ),
        )

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


def _load_local_dotenv() -> None:
    """读取项目 .env，但永不覆盖已由用户 shell 提供的环境变量。"""
    dotenv = Path.cwd() / ".env"
    if not dotenv.is_file():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def _environment_int(name: str, default: int) -> int:
    """Read an optional integer override while keeping defaults explicit."""
    value = os.environ.get(name)
    return default if value is None else int(value)


def _environment_float(name: str, default: float) -> float:
    """Read an optional float override while keeping defaults explicit."""
    value = os.environ.get(name)
    return default if value is None else float(value)


def _environment_bool(name: str, default: bool) -> bool:
    """Read an optional boolean override while keeping defaults explicit."""
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {value!r}")


def _environment_list(name: str, default: list[str]) -> list[str]:
    """Read an optional comma-separated list override, trimming entries."""
    value = os.environ.get(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]
