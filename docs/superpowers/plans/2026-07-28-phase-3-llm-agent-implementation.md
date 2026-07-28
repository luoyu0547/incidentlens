# IncidentLens Phase 3 LLM Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a real, recoverable IncidentLens LLM investigation agent that reads user-supplied OpenAI-compatible DeepSeek/GLM configuration, chooses audited read-only tools, progressively loads all five Skills, and passes both deterministic guards and live provider verification.

**Architecture:** LangChain `create_agent` owns the standard model/tool loop, LangGraph owns the single persisted execution state keyed by `incident_id`, and the existing Pydantic investigation models remain the validation/API projection. `ModelRegistry`, `SkillRuntime`, LangChain tool adapters, and deterministic evidence/report gates are separate boundaries so providers and the pre-1.0 Skills dependency can change without altering investigation logic.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2.13.4, LangChain 1.3.14, LangGraph 1.2.9, langchain-openai 1.4.1, langgraph-checkpoint-sqlite 3.1.0, DeepAgents 0.6.12, SQLAlchemy, SQLite, httpx, Docker Compose, pytest, Ruff, mypy, uv.

## Global Constraints

- Use `langchain>=1.3,<2` locked to `1.3.14`.
- Use `langgraph>=1.2,<2` locked to `1.2.9`.
- Use `langchain-openai>=1.4,<2` locked to `1.4.1`.
- Use `langgraph-checkpoint-sqlite>=3.1,<4` locked to `3.1.0`.
- Use `deepagents==0.6.12` only behind the internal `SkillRuntime` boundary.
- Use `pydantic>=2.13,<3` locked to `2.13.4`.
- IncidentLens source code must not import `openai`, hand-write model HTTP requests, parse raw provider tool calls, or implement a custom ReAct loop.
- Do not use `LLMChain`, `ConversationChain`, `langchain-classic`, `langgraph.prebuilt.create_react_agent`, PydanticAI runtime, `create_deep_agent`, Subagents, Todo middleware, Shell, or arbitrary filesystem writes.
- The two explicit modes are `llm_agent` and `deterministic_baseline`; default application and demo mode is `llm_agent`.
- Missing or invalid LLM configuration in `llm_agent` mode fails startup; it must never select a fake model or deterministic baseline automatically.
- Unit tests must not access the network; fake chat models are permitted only through explicit dependency injection.
- Model connect timeout defaults to 15 seconds, read timeout to 300 seconds, model transport retries to 2, total investigation timeout to 1200 seconds, maximum model calls to 12, maximum tool calls to 12, and maximum rounds to 8.
- The registry is the only owner of model transport retries; do not also add `ModelRetryMiddleware`.
- The existing read-only tool timeout remains 3 seconds with at most one internal retry.
- Deliver all five Skills in this plan: `downstream-timeout`, `downstream-error`, `database-pool-exhaustion`, `dependency-unavailable`, and `deployment-regression`.
- Skills may read only `/skills/**`; all other reads and every write are denied; no `execute` tool is exposed.
- Historical cases are low-confidence priors only and cannot satisfy the current-incident Evidence ID gate.
- LLM output cannot generate Evidence IDs, claim unexecuted tools, confirm a root cause by itself, or bypass `can_generate_report`.
- Live provider tests disable fallback and use the same `ModelRegistry` as production.
- If code tests pass but live verification does not, report exactly: `Agent implementation complete; live provider verification pending.`
- Do not declare Phase 3 complete until one configured provider passes the canary tool-call contract and at least one Compose scenario completes through a real model, Skill read, real tool execution, current Evidence, and guarded report.

---

## File Structure

### New configuration and model boundary

- `config/models.yaml`: checked-in non-secret DeepSeek and GLM profiles, active profile, fallback list, timeouts, and retry counts.
- `apps/control-plane/src/incidentlens_control_plane/llm/__init__.py`: public exports for model configuration and construction.
- `apps/control-plane/src/incidentlens_control_plane/llm/config.py`: strict Pydantic YAML/environment loading with secret resolution and mode-aware failure.
- `apps/control-plane/src/incidentlens_control_plane/llm/registry.py`: the only `ChatOpenAI` construction point; returns `BaseChatModel`.
- `apps/control-plane/src/incidentlens_control_plane/llm/fallback.py`: transport-only predicate and guarded subclass of LangChain `ModelFallbackMiddleware`.
- `apps/control-plane/src/incidentlens_control_plane/llm/canary.py`: reusable nonce tool-call probe used by live tests and manual diagnosis.

### New Agent runtime boundary

- `apps/control-plane/src/incidentlens_control_plane/agent/types.py`: runtime mode, typed LangGraph state, runtime context, terminal/error codes, and structured LLM proposal models.
- `apps/control-plane/src/incidentlens_control_plane/agent/projection.py`: the only raw LangGraph state to Pydantic `InvestigationState` projection.
- `apps/control-plane/src/incidentlens_control_plane/agent/checkpoint.py`: `AsyncSqliteSaver` lifecycle and latest-thread inspection helpers.
- `apps/control-plane/src/incidentlens_control_plane/agent/tool_adapter.py`: seven LangChain `StructuredTool` adapters, normalized call keys, tool-result serialization, Evidence creation, and dedup.
- `apps/control-plane/src/incidentlens_control_plane/agent/skills.py`: all DeepAgents imports, strict Skill discovery/validation, read-only filesystem middleware, policy loading, and Skill audit.
- `apps/control-plane/src/incidentlens_control_plane/agent/middleware.py`: model/tool/Skill audit, redaction, budgets, one structured repair, and current-incident report gate hooks.
- `apps/control-plane/src/incidentlens_control_plane/agent/prompts.py`: stable system prompt and per-round incident/evidence summary builders.
- `apps/control-plane/src/incidentlens_control_plane/agent/graph.py`: LangChain `create_agent` composition over LangGraph with checkpointer and bounded middleware.
- `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`: async `LLMInvestigationEngine` start/run/resume facade and total-timeout behavior.
- `apps/control-plane/src/incidentlens_control_plane/agent/baseline.py`: renamed second-stage deterministic engine retained only for explicit evaluation mode.
- `apps/control-plane/src/incidentlens_control_plane/agent/factory.py`: mode-aware engine construction; no API-key-based mode selection.

### Existing files to migrate

- `apps/control-plane/src/incidentlens_control_plane/agent/state.py`: keep Pydantic state and audit store; stop using/deleting the custom checkpoint table for LLM execution.
- `apps/control-plane/src/incidentlens_control_plane/agent/engine.py`: compatibility re-exports only after deterministic implementation moves to `baseline.py`.
- `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py`: remain deterministic normalization, not tool selection.
- `apps/control-plane/src/incidentlens_control_plane/agent/reporting.py`: accept only validated current-incident evidence and Skill policy.
- `apps/control-plane/src/incidentlens_control_plane/main.py`: FastAPI lifespan opens/closes async checkpoint resources and installs the selected runtime.
- `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`: await async start, expose mode/profile/error/checkpoint metadata, and publish model/Skill events.
- `apps/control-plane/src/incidentlens_control_plane/events.py`: add documented safe event types without serializing framework objects or secrets.
- `packages/demo/src/incidentlens_demo/runner.py`: 20-minute client budget, mode/profile assertions, and live trace contract.
- `infra/compose/Dockerfile`: include `config/` and `skills/` in the image.
- `infra/compose/compose.yaml`: pass explicit mode/config/key variables and mount config/Skills read-only for development.
- `.env.example`: document DeepSeek/GLM keys, profile override, mode, config path, and checkpoint path.
- `pyproject.toml`, `apps/control-plane/pyproject.toml`, `uv.lock`: add and lock the frozen Agent dependencies and pytest markers.

### New Skills

Each directory contains `SKILL.md`, `evidence-policy.yaml`, and the listed reference:

- `skills/downstream-timeout/references/trace-latency-guide.md`
- `skills/downstream-error/references/error-correlation-guide.md`
- `skills/database-pool-exhaustion/references/pool-saturation-guide.md`
- `skills/dependency-unavailable/references/dependency-health-guide.md`
- `skills/deployment-regression/references/change-correlation-guide.md`

### Tests

- `tests/conftest.py`: baseline mode for non-live test collection plus a hard network guard.
- `tests/support/fake_chat_model.py`: explicit scripted `BaseChatModel` used only by unit/graph tests.
- `tests/agent/conftest.py`: shared in-memory repositories, audit store, toolkit, graph/runtime, and recovery harnesses.
- `tests/web/conftest.py`: injected async Agent API and secret-redaction harnesses.
- `tests/llm/test_config.py`: strict config and environment override behavior.
- `tests/llm/test_registry.py`: constructor arguments, provider switching, fallback predicate, and forbidden direct SDK imports.
- `tests/live_llm/test_model_contract.py`: same-registry real canary test with nonce and no fallback.
- `tests/agent/test_langgraph_state.py`: state projection and SQLite checkpoint identity.
- `tests/agent/test_tool_adapter.py`: LangChain tool invocation, Evidence ownership, dedup, and invalid parameters.
- `tests/agent/test_skills.py`: all five Skills, metadata, progressive disclosure, permissions, policy, and audit.
- `tests/agent/test_llm_graph.py`: fake-model model/tool loop, no fixed strategy, policy gate, budgets, and invalid output.
- `tests/agent/test_recovery.py`: interrupt after a real tool and resume without duplicate execution.
- `tests/agent/test_runtime.py`: timeout, mode construction, terminal semantics, and checkpoint corruption.
- `tests/web/test_investigation_agent_api.py`: async API, mode/profile/error fields, and secret-safe SSE.
- `tests/integration/test_live_agent_compose.py`: one required real-model Compose path and five opt-in live scenarios.

---

### Task 1: Lock the Agent Ecosystem and Build Strict Model Configuration

**Files:**
- Create: `config/models.yaml`
- Create: `apps/control-plane/src/incidentlens_control_plane/llm/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/llm/config.py`
- Create: `tests/conftest.py`
- Create: `tests/llm/test_config.py`
- Modify: `pyproject.toml`
- Modify: `apps/control-plane/pyproject.toml`
- Modify: `uv.lock`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `Path`, `Mapping[str, str]`, Pydantic v2, PyYAML.
- Produces: `RuntimeMode`, `ModelProfile`, `ModelsConfig`, `ResolvedModelProfile`, `load_models_config(path: Path, environ: Mapping[str, str]) -> ModelsConfig`, and `resolve_model_profile(config: ModelsConfig, profile_name: str, environ: Mapping[str, str]) -> ResolvedModelProfile`.

- [ ] **Step 1: Write failing dependency and configuration tests**

```python
# tests/llm/test_config.py
from pathlib import Path

import pytest
from pydantic import ValidationError

from incidentlens_control_plane.llm.config import (
    RuntimeMode,
    load_models_config,
    resolve_model_profile,
)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_environment_selects_glm_and_resolves_only_named_secret(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
active_model: deepseek
models:
  deepseek:
    adapter: openai_compatible
    model: deepseek-chat
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
  glm:
    adapter: openai_compatible
    model: glm-4.5
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key_env: GLM_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
    )
    environ = {
        "INCIDENTLENS_LLM_ACTIVE_MODEL": "glm",
        "GLM_API_KEY": "glm-secret",
        "DEEPSEEK_API_KEY": "deepseek-secret",
    }

    config = load_models_config(path, environ)
    resolved = resolve_model_profile(config, config.active_model, environ)

    assert config.active_model == "glm"
    assert resolved.name == "glm"
    assert resolved.api_key.get_secret_value() == "glm-secret"
    assert "glm-secret" not in repr(resolved)


@pytest.mark.parametrize(
    "body",
    [
        "active_model: missing\nmodels: {}\nfallback_models: []\n",
        "active_model: deepseek\nmodels:\n  deepseek:\n    adapter: openai_compatible\n    model: ''\n    base_url: not-a-url\n    api_key_env: DEEPSEEK_API_KEY\nfallback_models: []\n",
        "active_model: deepseek\nmodels:\n  deepseek:\n    adapter: openai_compatible\n    model: deepseek-chat\n    base_url: https://api.deepseek.com\n    api_key_env: DEEPSEEK_API_KEY\n    unknown: true\nfallback_models: []\n",
    ],
)
def test_invalid_model_config_fails(tmp_path: Path, body: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        load_models_config(write_config(tmp_path, body), {})


def test_llm_mode_rejects_missing_secret_but_baseline_does_not_resolve_it(
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        """
active_model: deepseek
models:
  deepseek:
    adapter: openai_compatible
    model: deepseek-chat
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
    )
    config = load_models_config(path, {})
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        resolve_model_profile(config, "deepseek", {})
    assert RuntimeMode.DETERMINISTIC_BASELINE.value == "deterministic_baseline"
```

Create `tests/conftest.py` with collection-safe baseline mode and a socket guard fixture:

```python
import os
import socket

import pytest

os.environ.setdefault("INCIDENTLENS_AGENT_MODE", "deterministic_baseline")


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if request.node.get_closest_marker("live_llm") or request.node.get_closest_marker("integration"):
        return

    def denied(*args, **kwargs):
        raise AssertionError("unit test attempted a real network connection")

    monkeypatch.setattr(socket, "create_connection", denied)
```

- [ ] **Step 2: Run the focused tests and confirm the missing module failure**

Run: `uv run pytest tests/llm/test_config.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: incidentlens_control_plane.llm`.

- [ ] **Step 3: Add the frozen dependencies and strict configuration models**

Add to root and control-plane runtime dependencies:

```toml
"langchain>=1.3,<2",
"langgraph>=1.2,<2",
"langchain-openai>=1.4,<2",
"langgraph-checkpoint-sqlite>=3.1,<4",
"deepagents==0.6.12",
"pydantic>=2.13,<3",
"pyyaml>=6.0,<7",
```

Add pytest markers:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "integration: requires Docker Compose or service processes",
    "live_llm: performs a real configured provider request",
]
```

Implement these strict models in `llm/config.py`:

```python
from enum import StrEnum
from pathlib import Path
from typing import Literal, Mapping

import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator


class RuntimeMode(StrEnum):
    LLM_AGENT = "llm_agent"
    DETERMINISTIC_BASELINE = "deterministic_baseline"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["openai_compatible"]
    model: str = Field(min_length=1)
    base_url: AnyHttpUrl
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    connect_timeout_seconds: float = Field(default=15, gt=0)
    read_timeout_seconds: float = Field(default=300, gt=0)
    max_retries: int = Field(default=2, ge=0, le=2)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_model: str = Field(min_length=1)
    models: dict[str, ModelProfile] = Field(min_length=1)
    fallback_models: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def names_exist(self) -> "ModelsConfig":
        referenced = [self.active_model, *self.fallback_models]
        missing = [name for name in referenced if name not in self.models]
        if missing:
            raise ValueError(f"unknown model profiles: {', '.join(missing)}")
        return self


class ResolvedModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    adapter: Literal["openai_compatible"]
    model: str
    base_url: AnyHttpUrl
    api_key: SecretStr
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int


def load_models_config(path: Path, environ: Mapping[str, str]) -> ModelsConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ModelsConfig.model_validate(raw)
    selected = environ.get("INCIDENTLENS_LLM_ACTIVE_MODEL", "").strip()
    if not selected:
        return config
    return ModelsConfig.model_validate(
        {**config.model_dump(mode="python"), "active_model": selected}
    )


def resolve_model_profile(
    config: ModelsConfig,
    profile_name: str,
    environ: Mapping[str, str],
) -> ResolvedModelProfile:
    if profile_name not in config.models:
        raise ValueError(f"unknown model profile: {profile_name}")
    profile = config.models[profile_name]
    secret = environ.get(profile.api_key_env, "").strip()
    if not secret:
        raise ValueError(f"missing or empty model secret: {profile.api_key_env}")
    return ResolvedModelProfile(
        name=profile_name,
        api_key=SecretStr(secret),
        **profile.model_dump(),
    )
```

Write `config/models.yaml` with the exact DeepSeek/GLM profiles frozen in the design, and add these non-secret entries to `.env.example`:

```dotenv
INCIDENTLENS_AGENT_MODE=llm_agent
INCIDENTLENS_MODELS_CONFIG=/app/config/models.yaml
INCIDENTLENS_LLM_ACTIVE_MODEL=deepseek
INCIDENTLENS_CHECKPOINT_DB=/data/agent_checkpoints.db
DEEPSEEK_API_KEY=
GLM_API_KEY=
```

- [ ] **Step 4: Lock dependencies and verify exact versions**

Run: `uv lock && uv sync`

Run:

```bash
uv run python -c "import importlib.metadata as m; print({n: m.version(n) for n in ['langchain','langgraph','langchain-openai','langgraph-checkpoint-sqlite','deepagents','pydantic']})"
```

Expected:

```text
{'langchain': '1.3.14', 'langgraph': '1.2.9', 'langchain-openai': '1.4.1', 'langgraph-checkpoint-sqlite': '3.1.0', 'deepagents': '0.6.12', 'pydantic': '2.13.4'}
```

- [ ] **Step 5: Run the configuration tests**

Run: `uv run pytest tests/llm/test_config.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the configuration boundary**

```bash
git add pyproject.toml apps/control-plane/pyproject.toml uv.lock config/models.yaml .env.example tests/conftest.py tests/llm/test_config.py apps/control-plane/src/incidentlens_control_plane/llm
git commit -m "feat: add strict llm model configuration"
```

### Task 2: Build the LangChain Model Registry, Safe Fallback, and Real Canary

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/llm/registry.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/llm/fallback.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/llm/canary.py`
- Create: `tests/llm/test_registry.py`
- Create: `tests/live_llm/test_model_contract.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/llm/__init__.py`

**Interfaces:**
- Consumes: `ModelsConfig`, `ResolvedModelProfile`, `BaseChatModel`, `ChatOpenAI`, LangChain `ModelFallbackMiddleware`.
- Produces: `ModelRegistry.get(profile_name: str | None = None) -> BaseChatModel`, `ModelRegistry.identity(profile_name: str | None = None) -> ModelIdentity`, `TransportOnlyModelFallbackMiddleware`, `run_model_canary(registry: ModelRegistry, profile_name: str) -> CanaryResult`.

- [ ] **Step 1: Write registry constructor and provider-switch tests**

```python
# tests/llm/test_registry.py
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from incidentlens_control_plane.llm.config import load_models_config
from incidentlens_control_plane.llm.fallback import is_retryable_transport_error
from incidentlens_control_plane.llm.registry import ModelRegistry


def test_registry_passes_endpoint_model_secret_timeout_and_single_retry_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class SentinelChatModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "incidentlens_control_plane.llm.registry.ChatOpenAI",
        SentinelChatModel,
    )
    config = load_models_config(Path("config/models.yaml"), {})
    registry = ModelRegistry(config, {"DEEPSEEK_API_KEY": "secret"})

    registry.get("deepseek")

    assert captured["model"] == "deepseek-chat"
    assert captured["base_url"] == "https://api.deepseek.com/"
    assert captured["api_key"].get_secret_value() == "secret"
    assert captured["max_retries"] == 2
    assert captured["timeout"].connect == 15
    assert captured["timeout"].read == 300


def test_switching_profile_changes_registry_only(monkeypatch: pytest.MonkeyPatch) -> None:
    models: list[str] = []

    class SentinelChatModel:
        def __init__(self, **kwargs):
            models.append(kwargs["model"])

    monkeypatch.setattr(
        "incidentlens_control_plane.llm.registry.ChatOpenAI",
        SentinelChatModel,
    )
    config = load_models_config(Path("config/models.yaml"), {})
    registry = ModelRegistry(
        config,
        {"DEEPSEEK_API_KEY": "d", "GLM_API_KEY": "g"},
    )

    registry.get("deepseek")
    registry.get("glm")

    assert models == ["deepseek-chat", "glm-4.5"]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("down"), True),
        (httpx.ReadTimeout("slow"), True),
        (type("StatusError", (Exception,), {"status_code": 429})(), True),
        (type("StatusError", (Exception,), {"status_code": 503})(), True),
        (type("StatusError", (Exception,), {"status_code": 401})(), False),
        (ValueError("invalid tool args"), False),
    ],
)
def test_fallback_predicate_allows_only_transport_429_and_5xx(exc, expected) -> None:
    assert is_retryable_transport_error(exc) is expected


def test_source_does_not_import_openai_sdk() -> None:
    source_root = Path("apps/control-plane/src/incidentlens_control_plane")
    offenders = [
        path
        for path in source_root.rglob("*.py")
        if "import openai" in path.read_text(encoding="utf-8")
        or "from openai" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
```

- [ ] **Step 2: Run registry tests and confirm missing implementation**

Run: `uv run pytest tests/llm/test_registry.py -q`

Expected: FAIL with missing `registry`, `fallback`, and `canary` modules.

- [ ] **Step 3: Implement the registry and identity redaction**

```python
# llm/registry.py
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import ModelsConfig, resolve_model_profile


@dataclass(frozen=True)
class ModelIdentity:
    profile: str
    model: str
    endpoint_host: str


class ModelRegistry:
    def __init__(self, config: ModelsConfig, environ: Mapping[str, str]) -> None:
        self._config = config
        self._environ = environ

    @property
    def active_profile(self) -> str:
        return self._config.active_model

    @property
    def fallback_profiles(self) -> tuple[str, ...]:
        return tuple(self._config.fallback_models)

    def get(self, profile_name: str | None = None) -> BaseChatModel:
        resolved = resolve_model_profile(
            self._config,
            profile_name or self.active_profile,
            self._environ,
        )
        timeout = httpx.Timeout(
            connect=resolved.connect_timeout_seconds,
            read=resolved.read_timeout_seconds,
            write=resolved.read_timeout_seconds,
            pool=resolved.connect_timeout_seconds,
        )
        return ChatOpenAI(
            model=resolved.model,
            base_url=str(resolved.base_url),
            api_key=resolved.api_key,
            timeout=timeout,
            max_retries=resolved.max_retries,
        )

    def identity(self, profile_name: str | None = None) -> ModelIdentity:
        name = profile_name or self.active_profile
        profile = self._config.models[name]
        return ModelIdentity(
            profile=name,
            model=profile.model,
            endpoint_host=urlsplit(str(profile.base_url)).netloc,
        )
```

Implement `TransportOnlyModelFallbackMiddleware` as a subclass of LangChain's middleware. Override sync and async wrappers so the primary error is re-raised unless `is_retryable_transport_error` traversing `__cause__`/`__context__` finds an `httpx.TransportError`, status 429, or 5xx. Use `request.override(model=fallback_model)` and never log request content.

- [ ] **Step 4: Write the live canary contract**

```python
# tests/live_llm/test_model_contract.py
import os
import secrets
from pathlib import Path

import pytest

from incidentlens_control_plane.llm.canary import run_model_canary
from incidentlens_control_plane.llm.config import load_models_config
from incidentlens_control_plane.llm.registry import ModelRegistry


@pytest.mark.live_llm
async def test_selected_profile_performs_real_required_tool_call() -> None:
    path = Path(os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml"))
    config = load_models_config(path, os.environ)
    profile = config.models[config.active_model]
    if profile.api_key_env not in os.environ:
        pytest.skip(f"missing {profile.api_key_env}")
    if not os.environ[profile.api_key_env].strip():
        pytest.fail(f"{profile.api_key_env} exists but is empty")

    registry = ModelRegistry(config, os.environ)
    result = await run_model_canary(registry, config.active_model)

    assert result.nonce
    assert result.tool_name == "incidentlens_canary"
    assert result.audit_nonce == result.nonce
    assert result.identity == registry.identity(config.active_model)
    assert result.fallback_used is False
```

Implement the canary with a random nonce supplied by the caller, a `StructuredTool.from_function` async function whose Pydantic input is `CanaryArgs(nonce: str)`, `model.bind_tools([tool], tool_choice="required")`, and normalized LangChain `AIMessage.tool_calls`. Execute the returned tool through LangChain's tool API and return `CanaryResult`; do not call provider-specific response fields.

- [ ] **Step 5: Run non-live tests, then verify missing-key skip semantics**

Run: `uv run pytest tests/llm/test_registry.py -q`

Expected: PASS.

Run: `env -u DEEPSEEK_API_KEY -u GLM_API_KEY uv run pytest tests/live_llm/test_model_contract.py -m live_llm -q`

Expected: one explicit SKIP naming the active profile's key environment variable.

- [ ] **Step 6: Commit model construction and canary**

```bash
git add apps/control-plane/src/incidentlens_control_plane/llm tests/llm/test_registry.py tests/live_llm/test_model_contract.py
git commit -m "feat: add langchain model registry and live canary"
```

### Task 3: Make LangGraph the Single Agent Checkpoint Source

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/projection.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/checkpoint.py`
- Create: `tests/agent/test_langgraph_state.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/state.py`

**Interfaces:**
- Consumes: LangChain `AgentState`, `AsyncSqliteSaver`, existing `Evidence`, `Hypothesis`, `InvestigationState`.
- Produces: `IncidentAgentState`, `InvestigationContext`, `RootCauseProposal`, `project_investigation_state(raw: Mapping[str, Any]) -> InvestigationState`, and `AgentCheckpointRuntime`.

- [ ] **Step 1: Write state projection and real SQLite checkpointer tests**

```python
# tests/agent/test_langgraph_state.py
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from incidentlens_control_plane.agent.checkpoint import AgentCheckpointRuntime
from incidentlens_control_plane.agent.projection import project_investigation_state
from incidentlens_control_plane.agent.types import IncidentAgentState


def test_projection_validates_domain_state() -> None:
    projected = project_investigation_state(
        {
            "incident_id": "inc-1",
            "status": "investigating",
            "current_round": 1,
            "max_rounds": 8,
            "alert": {"service": "order-service"},
            "hypotheses": [],
            "evidence": [],
            "report": None,
            "phase": "agent_loop",
            "retrieved_cases": [],
            "loaded_skill_names": ["downstream-timeout"],
            "model_profile": "deepseek",
            "model_call_count": 1,
            "tool_call_count": 0,
            "fallback_used": False,
        }
    )
    assert projected.incident_id == "inc-1"
    assert projected.status.value == "investigating"


async def test_sqlite_checkpoint_uses_incident_id_as_thread_id(tmp_path: Path) -> None:
    async with AgentCheckpointRuntime(tmp_path / "agent.db") as checkpoints:
        builder = StateGraph(IncidentAgentState)
        builder.add_node("advance", lambda state: {"current_round": 1})
        builder.add_edge(START, "advance")
        builder.add_edge("advance", END)
        graph = builder.compile(checkpointer=checkpoints.saver)
        config = checkpoints.config_for("inc-1")
        await graph.ainvoke(
            {
                "messages": [],
                "incident_id": "inc-1",
                "current_round": 0,
            },
            config,
        )
        saved = await graph.aget_state(config)
        assert saved.config["configurable"]["thread_id"] == "inc-1"
        assert saved.values["current_round"] == 1
```

- [ ] **Step 2: Run the focused test and confirm missing types**

Run: `uv run pytest tests/agent/test_langgraph_state.py -q`

Expected: FAIL with missing `agent.types`, `agent.projection`, and `agent.checkpoint`.

- [ ] **Step 3: Define the TypedDict execution state and Pydantic proposal**

```python
# agent/types.py
import operator
from typing import Annotated, Any, NotRequired

from incidentlens_contracts.models import Evidence, Hypothesis
from langchain.agents.middleware import AgentState
from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.llm.config import RuntimeMode


def merge_evidence(left: list[Evidence], right: list[Evidence]) -> list[Evidence]:
    merged = {item.id: item for item in left}
    merged.update({item.id: item for item in right})
    return list(merged.values())


def merge_unique_strings(left: list[str], right: list[str]) -> list[str]:
    return list(dict.fromkeys([*left, *right]))


class IncidentAgentState(AgentState):
    incident_id: str
    status: str
    phase: str
    alert: dict[str, Any]
    current_round: int
    max_rounds: int
    hypotheses: list[Hypothesis]
    evidence: Annotated[list[Evidence], merge_evidence]
    retrieved_cases: list[dict[str, Any]]
    loaded_skill_names: Annotated[list[str], merge_unique_strings]
    model_profile: str
    model_call_count: Annotated[int, operator.add]
    tool_call_count: Annotated[int, operator.add]
    fallback_used: bool
    report: dict[str, Any] | None
    last_error_code: NotRequired[str | None]
    last_checkpoint_id: NotRequired[str | None]


class InvestigationContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    incident_id: str
    mode: RuntimeMode


class RootCauseProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_service: str = Field(min_length=1)
    cause_code: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    next_action: str = Field(min_length=1)
```

Extend `InvestigationState` with exact defaults:

```python
loaded_skill_names: list[str] = Field(default_factory=list)
model_profile: str = ""
model_call_count: int = 0
tool_call_count: int = 0
fallback_used: bool = False
last_error_code: str | None = None
last_checkpoint_id: str | None = None
```

Remove the duplicated `incident_id=` argument in `CheckpointStore.load`. Mark `CheckpointStore` as deterministic-baseline compatibility only in its docstring.

- [ ] **Step 4: Implement checkpoint lifecycle and projection**

`AgentCheckpointRuntime` must open `AsyncSqliteSaver.from_conn_string(str(path))` in `__aenter__`, close it in `__aexit__`, expose `saver`, and return:

```python
def config_for(self, incident_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": incident_id}}
```

`project_investigation_state` must use `InvestigationState.model_validate` after validating each raw `Hypothesis` and `Evidence`; it must not fill absent required Agent fields from a second database.

- [ ] **Step 5: Run state tests and existing contract tests**

Run: `uv run pytest tests/agent/test_langgraph_state.py tests/contracts/test_models.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the state and checkpoint boundary**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent/types.py apps/control-plane/src/incidentlens_control_plane/agent/projection.py apps/control-plane/src/incidentlens_control_plane/agent/checkpoint.py apps/control-plane/src/incidentlens_control_plane/agent/state.py tests/agent/test_langgraph_state.py
git commit -m "feat: add langgraph investigation checkpoints"
```

### Task 4: Adapt the Seven Existing Read-Only Tools to LangChain and Current-Incident Evidence

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/tool_adapter.py`
- Create: `tests/agent/test_tool_adapter.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/tools/query.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `ReadOnlyToolkit`, existing Pydantic argument models, `ToolResult`, `Evidence`, `InvestigationAuditStore`.
- Produces: `build_agent_tools(toolkit: ReadOnlyToolkit, evidence_recorder: EvidenceRecorder) -> list[BaseTool]`, `EvidenceRecorder.record(...) -> Evidence`, and `normalize_tool_call_key(incident_id: str, tool_name: str, arguments: BaseModel) -> str`.

- [ ] **Step 1: Write failing LangChain tool and Evidence ownership tests**

```python
# append to tests/conftest.py
import pytest
from incidentlens_telemetry.database import create_engine
from incidentlens_telemetry.repository import TelemetryRepository

from incidentlens_control_plane.agent.state import InvestigationAuditStore
from incidentlens_control_plane.tools.query import ReadOnlyToolkit


@pytest.fixture
def telemetry_repo() -> TelemetryRepository:
    return TelemetryRepository(create_engine("sqlite:///:memory:"))


@pytest.fixture
def toolkit(telemetry_repo: TelemetryRepository) -> ReadOnlyToolkit:
    return ReadOnlyToolkit(telemetry_repo)


@pytest.fixture
def investigation_audit_store(
    telemetry_repo: TelemetryRepository,
) -> InvestigationAuditStore:
    return InvestigationAuditStore(telemetry_repo.engine)
```

```python
# tests/agent/test_tool_adapter.py
import pytest
from pydantic import ValidationError

from incidentlens_control_plane.agent.tool_adapter import (
    EvidenceRecorder,
    build_agent_tools,
)


async def test_agent_tool_executes_existing_tool_and_records_owned_evidence(
    toolkit,
    investigation_audit_store,
) -> None:
    recorder = EvidenceRecorder(investigation_audit_store)
    tools = {tool.name: tool for tool in build_agent_tools(toolkit, recorder)}

    response = await tools["search_logs"].ainvoke(
        {
            "name": "search_logs",
            "args": {
                "incident_id": "inc-1",
                "service": "order-service",
                "keyword": "timeout",
                "limit": 10,
            },
            "id": "call-1",
            "type": "tool_call",
        }
    )
    result = response.artifact

    assert result["tool_result"]["ok"] is True
    assert result["evidence"]["id"]
    assert result["evidence"]["content"]["incident_id"] == "inc-1"
    assert result["evidence"]["source_tool"] == "search_logs"


async def test_duplicate_normalized_call_reuses_evidence_id(toolkit, investigation_audit_store) -> None:
    recorder = EvidenceRecorder(investigation_audit_store)
    tool = {tool.name: tool for tool in build_agent_tools(toolkit, recorder)}["search_logs"]
    args = {
        "incident_id": "inc-1",
        "service": "order-service",
        "keyword": "timeout",
        "limit": 10,
    }
    first_response = await tool.ainvoke(
        {"name": "search_logs", "args": args, "id": "call-1", "type": "tool_call"}
    )
    second_response = await tool.ainvoke(
        {"name": "search_logs", "args": args, "id": "call-2", "type": "tool_call"}
    )
    first = first_response.artifact
    second = second_response.artifact

    assert second["deduplicated"] is True
    assert second["evidence"]["id"] == first["evidence"]["id"]


async def test_invalid_tool_args_are_rejected_before_repository(
    toolkit,
    investigation_audit_store,
) -> None:
    recorder = EvidenceRecorder(investigation_audit_store)
    tool = {tool.name: tool for tool in build_agent_tools(toolkit, recorder)}["query_metrics"]
    with pytest.raises(ValidationError):
        await tool.ainvoke(
            {
                "name": "query_metrics",
                "args": {
                    "incident_id": "inc-1",
                    "service": "",
                    "limit": 1000,
                },
                "id": "call-invalid",
                "type": "tool_call",
            }
        )
```

- [ ] **Step 2: Run the focused tests and confirm missing adapter**

Run: `uv run pytest tests/agent/test_tool_adapter.py -q`

Expected: FAIL with missing `agent.tool_adapter`.

- [ ] **Step 3: Implement explicit Pydantic adapter inputs**

Define one Agent-facing model per tool. Each includes `incident_id: str` and the existing bounded fields; do not expose repository objects, arbitrary query strings, or root-cause labels. Build tools with `StructuredTool.from_function(coroutine=..., args_schema=..., response_format="content_and_artifact")`. Each coroutine returns `(bounded_summary, envelope.model_dump(mode="json"))`, allowing LangChain to place the machine envelope in `ToolMessage.artifact`.

```python
class AgentToolEnvelope(BaseModel):
    tool_result: ToolResult[Any]
    evidence: Evidence
    deduplicated: bool = False


class EvidenceRecorder:
    def record(
        self,
        *,
        incident_id: str,
        tool_name: str,
        normalized_args: dict[str, Any],
        result: ToolResult[Any],
    ) -> tuple[Evidence, bool]:
        call_key = stable_sha256(incident_id, tool_name, normalized_args)
        existing = self._find_by_call_key(incident_id, call_key)
        if existing is not None:
            return existing, True
        evidence = Evidence(
            id=f"ev-{call_key[:16]}",
            source_tool=tool_name,
            tool_call_id=call_key,
            content={
                "incident_id": incident_id,
                "outcome": "success" if result.ok else "tool_error",
                "data": result.data,
                "error": result.error,
                "metadata": result.metadata,
            },
        )
        self._audit_store.record(
            incident_id,
            "evidence_recorded",
            {"call_key": call_key, "evidence": evidence.model_dump(mode="json")},
        )
        return evidence, False
```

The adapter must call the existing `ReadOnlyToolkit`; do not copy query logic. At this boundary invalid Pydantic input is rejected before repository execution. Task 6's single tool-error middleware converts that validation failure into one `invalid_arguments` Evidence entry and one repairable model message.

- [ ] **Step 4: Run adapter and original tool suites**

Run: `uv run pytest tests/agent/test_tool_adapter.py tests/tools/test_read_only_tools.py -q`

Expected: PASS, including the original 3-second timeout and one-retry assertions.

- [ ] **Step 5: Commit the LangChain tool boundary**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent/tool_adapter.py apps/control-plane/src/incidentlens_control_plane/tools/query.py tests/conftest.py tests/agent/test_tool_adapter.py
git commit -m "feat: adapt read-only tools for langchain agents"
```

### Task 5: Deliver All Five Skills, Machine Policies, Progressive Loading, and Read-Only Permissions

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/skills.py`
- Create: `skills/downstream-timeout/SKILL.md`
- Create: `skills/downstream-timeout/evidence-policy.yaml`
- Create: `skills/downstream-timeout/references/trace-latency-guide.md`
- Create: `skills/downstream-error/SKILL.md`
- Create: `skills/downstream-error/evidence-policy.yaml`
- Create: `skills/downstream-error/references/error-correlation-guide.md`
- Create: `skills/database-pool-exhaustion/SKILL.md`
- Create: `skills/database-pool-exhaustion/evidence-policy.yaml`
- Create: `skills/database-pool-exhaustion/references/pool-saturation-guide.md`
- Create: `skills/dependency-unavailable/SKILL.md`
- Create: `skills/dependency-unavailable/evidence-policy.yaml`
- Create: `skills/dependency-unavailable/references/dependency-health-guide.md`
- Create: `skills/deployment-regression/SKILL.md`
- Create: `skills/deployment-regression/evidence-policy.yaml`
- Create: `skills/deployment-regression/references/change-correlation-guide.md`
- Create: `tests/agent/test_skills.py`

**Interfaces:**
- Consumes: DeepAgents `FilesystemBackend`, `FilesystemMiddleware`, `FilesystemPermission`, `SkillsMiddleware`; `InvestigationAuditStore`.
- Produces: `EvidencePolicy`, `SkillDefinition`, `SkillRuntime.validate() -> tuple[SkillDefinition, ...]`, `SkillRuntime.middleware() -> tuple[AgentMiddleware, AgentMiddleware, AgentMiddleware]`, and `SkillRuntime.policy_for(cause_code: str) -> EvidencePolicy`.

- [ ] **Step 1: Write the all-at-once Skill contract tests**

```python
# tests/agent/test_skills.py
from pathlib import Path

import pytest

from incidentlens_control_plane.agent.skills import SkillRuntime

EXPECTED = {
    "downstream-timeout",
    "downstream-error",
    "database-pool-exhaustion",
    "dependency-unavailable",
    "deployment-regression",
}


def test_all_five_skills_are_validated_together(investigation_audit_store) -> None:
    runtime = SkillRuntime(Path("skills"), investigation_audit_store)
    definitions = runtime.validate()
    assert {item.name for item in definitions} == EXPECTED
    assert all(item.policy.minimum_independent_evidence >= 2 for item in definitions)
    assert all(item.reference_paths for item in definitions)


def test_initial_skill_prompt_contains_metadata_not_full_body(investigation_audit_store) -> None:
    runtime = SkillRuntime(Path("skills"), investigation_audit_store)
    prompt = runtime.metadata_prompt()
    assert "downstream-timeout" in prompt
    assert "## Stop conditions" not in prompt
    assert "trace-latency-guide.md" not in prompt


async def test_backend_allows_skill_reads_and_denies_everything_else(
    investigation_audit_store,
) -> None:
    runtime = SkillRuntime(Path("skills"), investigation_audit_store)
    assert (await runtime.read_file("/skills/downstream-timeout/SKILL.md")).ok
    assert not (await runtime.read_file("/etc/passwd")).ok
    assert not (await runtime.write_file("/skills/downstream-timeout/x.md", "x")).ok
    assert not (await runtime.read_file("/skills/../config/models.yaml")).ok


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("duplicate_name", "duplicate skill name"),
        ("unknown_tool", "unknown allowed tool"),
        ("missing_policy", "evidence-policy.yaml"),
        ("missing_frontmatter", "frontmatter"),
        ("path_traversal", "path traversal"),
    ],
)
def test_invalid_skill_fails_startup(
    tmp_path: Path,
    mutation: str,
    message: str,
    investigation_audit_store,
) -> None:
    build_invalid_skill_tree(tmp_path, mutation)
    with pytest.raises(ValueError, match=message):
        SkillRuntime(tmp_path, investigation_audit_store).validate()


def build_invalid_skill_tree(root: Path, mutation: str) -> None:
    valid = """---
name: downstream-timeout
description: Diagnose downstream timeout symptoms.
license: MIT
compatibility: IncidentLens phase 3
metadata:
  version: "1.0.0"
allowed-tools: read_file search_logs
---
# Skill
## Applicable symptoms
## Investigation order
## Candidate hypothesis
## Minimum supporting evidence
## Contradictions
## Stop conditions
## Forbidden behavior
"""
    skill = root / "downstream-timeout"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(valid, encoding="utf-8")
    (skill / "evidence-policy.yaml").write_text(
        "skill_name: downstream-timeout\n"
        "cause_code: payment_latency_spike\n"
        "required_evidence_types: [trace, log]\n"
        "minimum_independent_evidence: 2\n"
        "direct_contradictions: [normal downstream latency]\n",
        encoding="utf-8",
    )
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    mutate_skill_tree(root, mutation)
```

In the same test file, implement `mutate_skill_tree` as a closed `match` over the five parametrized names: copy the valid directory to `duplicate/` and retain the same frontmatter name for `duplicate_name`; replace `search_logs` with `delete_database` for `unknown_tool`; unlink `evidence-policy.yaml` for `missing_policy`; remove both `---` delimiters for `missing_frontmatter`; and create `root.parent / "outside.md"` plus a `references/escape.md` symlink to that outside file for `path_traversal`. Raise `AssertionError(mutation)` for any other value. The validator checks duplicate names before directory/name mismatch so the duplicate fixture produces the asserted error.

- [ ] **Step 2: Run the Skill tests and confirm all five are absent**

Run: `uv run pytest tests/agent/test_skills.py -q`

Expected: FAIL because `SkillRuntime` and the `skills/` tree do not exist.

- [ ] **Step 3: Create the exact Skill metadata and policies**

Use this exact frontmatter matrix; `allowed-tools` is a space-delimited string because DeepAgents 0.6.12 parses that form:

| Skill | Description | Allowed tools | Cause code | Required evidence types | Direct contradiction |
| --- | --- | --- | --- | --- | --- |
| `downstream-timeout` | Diagnose upstream latency and 5xx caused by slow downstream spans or timeout behavior. Use when traces or logs indicate a slow dependency. | `read_file get_service_dependencies get_slow_traces get_trace search_logs query_metrics get_runbook` | `payment_latency_spike` | `trace`, `log`, `metric` | downstream span latency is normal in the incident window |
| `downstream-error` | Diagnose upstream failures caused by an elevated downstream application error rate. Use when dependency spans and logs contain correlated errors. | `read_file get_service_dependencies get_trace search_logs query_metrics get_runbook` | `payment_service_degradation` | `trace`, `log`, `metric` | downstream success and error rates remain at baseline |
| `database-pool-exhaustion` | Diagnose request failures caused by database connection acquisition saturation. Use when pool timeout logs or pool metrics are present. | `read_file search_logs query_metrics get_slow_traces get_trace get_runbook` | `database_connection_leak` | `log`, `metric`, `trace` | available pool capacity remains healthy during failed requests |
| `dependency-unavailable` | Diagnose an unreachable service or network dependency. Use when connection failures and broken dependency traces occur. | `read_file get_service_dependencies search_logs query_metrics get_trace get_runbook` | `network_partition` | `log`, `trace`, `metric` | successful dependency calls continue through the same incident window |
| `deployment-regression` | Diagnose a failure correlated with a recent version or configuration deployment. Use only when current telemetry and a change record align. | `read_file list_recent_deployments search_logs query_metrics get_slow_traces get_trace get_runbook` | `bad_deployment` | `deployment`, `log`, `trace` | the same failure predates the candidate deployment |

Every `SKILL.md` must contain these exact headings with Skill-specific content from the matrix:

```markdown
---
name: downstream-timeout
description: Diagnose upstream latency and 5xx caused by slow downstream spans or timeout behavior. Use when traces or logs indicate a slow dependency.
license: MIT
compatibility: IncidentLens phase 3; read-only observability tools
metadata:
  version: "1.0.0"
allowed-tools: read_file get_service_dependencies get_slow_traces get_trace search_logs query_metrics get_runbook
---

# Downstream timeout investigation

## Applicable symptoms
- Upstream P95 latency and 5xx rise in the same incident window.
- One downstream span accounts for most of the slow trace duration.

## Investigation order
1. Read `references/trace-latency-guide.md`.
2. Query service dependencies and identify the downstream edge.
3. Query slow traces, then inspect a representative trace.
4. Correlate timeout logs and latency/error metrics for the same time window.

## Candidate hypothesis
The upstream failure is caused by latency or timeout behavior in a downstream service.

## Minimum supporting evidence
- A current-incident slow trace identifies the downstream span.
- A second independent current source is abnormal: correlated logs or metrics.
- A report may cite only Evidence IDs returned by executed tools.

## Contradictions
- Downstream span latency is normal in the incident window.
- The delay occurs entirely before or after the downstream call.

## Stop conditions
- Stop with insufficient evidence when two independent current sources cannot be obtained.
- Stop and reject this hypothesis when a direct contradiction remains unresolved.

## Forbidden behavior
- Do not treat a historical case as current proof.
- Do not invent Evidence IDs or claim an unexecuted query.
- Do not request writes, Shell, restarts, rollbacks, or configuration changes.
```

Create the other four files with their own frontmatter, symptoms, investigation order, candidate hypothesis, minimum evidence, contradiction, stop, and forbidden sections using the exact matrix values. Each reference explains how to interpret its named evidence source and includes no commands or secrets.

Each `evidence-policy.yaml` uses:

```yaml
skill_name: downstream-timeout
cause_code: payment_latency_spike
required_evidence_types:
  - trace
  - log
  - metric
minimum_independent_evidence: 2
direct_contradictions:
  - downstream span latency is normal in the incident window
```

Substitute only the exact row values for the other four policy files.

- [ ] **Step 4: Implement the isolated DeepAgents adapter and strict validator**

Import all DeepAgents classes only in `agent/skills.py`:

```python
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.state import StateBackend
from deepagents.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemPermission,
)
from deepagents.middleware.skills import SkillsMiddleware
```

Build the filesystem backend at the physical `skills_root`, then expose it only through the virtual `/skills/` route:

```python
skills_backend = FilesystemBackend(root_dir=skills_root, virtual_mode=True)
backend = CompositeBackend(
    default=StateBackend(),
    routes={"/skills/": skills_backend},
)
permissions = [
    FilesystemPermission(operations=["read"], paths=["/skills/**"], mode="allow"),
    FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]
filesystem = FilesystemMiddleware(backend=backend, _permissions=permissions)
skills = SkillsMiddleware(backend=backend, sources=[("/skills/", "IncidentLens")])
```

The locked 0.6.12 permission constructor currently uses the private `_permissions` parameter. Pin it behind `SkillRuntime`, and make the contract test fail on an upstream signature change. `SkillRuntime.middleware()` returns `(filesystem, skills, skill_read_audit)`. `skill_read_audit` records only normalized `/skills/...` reads using `ToolCallRequest.runtime.context.incident_id`; startup discovery records `skill_scan` with names and paths but not full Skill bodies. Validate path containment with `Path.resolve().is_relative_to(skills_root.resolve())`, reject duplicate names, unknown tools, missing files, description over 1024 characters, and mismatched directory/name. Validate `evidence-policy.yaml` with `extra="forbid"` and ensure every `cause_code` is unique.

- [ ] **Step 5: Run the complete Skill contract**

Run: `uv run pytest tests/agent/test_skills.py -q`

Expected: PASS for discovery, progressive disclosure, read permissions, write denial, invalid fixtures, and policy mapping.

- [ ] **Step 6: Commit all five Skills in one commit**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent/skills.py skills tests/agent/test_skills.py
git commit -m "feat: add complete incident investigation skills"
```

### Task 6: Compose the Bounded LangChain Agent Graph and Deterministic Report Gate

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/prompts.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/middleware.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/graph.py`
- Create: `tests/support/__init__.py`
- Create: `tests/support/fake_chat_model.py`
- Create: `tests/agent/conftest.py`
- Create: `tests/agent/test_llm_graph.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/reporting.py`

**Interfaces:**
- Consumes: `ModelRegistry`, `SkillRuntime`, seven Agent tools, `IncidentAgentState`, checkpointer, existing evidence normalization.
- Produces: `build_investigation_agent(model: BaseChatModel, tools: Sequence[BaseTool], skill_runtime: SkillRuntime, checkpointer: BaseCheckpointSaver, audit_store: InvestigationAuditStore, fallback_models: Sequence[BaseChatModel] = (), allow_fallback: bool = True) -> CompiledStateGraph`, `can_generate_guarded_report(state: IncidentAgentState, proposal: RootCauseProposal, policies: Mapping[str, EvidencePolicy]) -> GuardDecision`, and audit-safe middleware.

- [ ] **Step 1: Write fake-model tests proving the model chooses tools**

```python
# tests/support/fake_chat_model.py
from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    responses: list[AIMessage]
    cursor: int = 0
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "incidentlens-scripted-test-model"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ScriptedChatModel":
        self.bound_tool_names = [
            tool.name if hasattr(tool, "name") else tool["function"]["name"]
            for tool in tools
        ]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.cursor >= len(self.responses):
            raise AssertionError("scripted model exhausted")
        message = self.responses[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])
```

Extend `tests/agent/conftest.py` with an `AgentHarness` that owns the real SQLite `AgentCheckpointRuntime`, real seven tool adapters, real `SkillRuntime`, and an injected `ScriptedChatModel`:

```python
@dataclass
class AgentHarness:
    checkpointer: Any
    tools: list[BaseTool]
    skills: SkillRuntime
    audit_store: InvestigationAuditStore

    def fake_model(self, responses: list[AIMessage]) -> ScriptedChatModel:
        return ScriptedChatModel(responses=responses)

    def build(self, model: BaseChatModel):
        return build_investigation_agent(
            model=model,
            tools=self.tools,
            skill_runtime=self.skills,
            checkpointer=self.checkpointer,
            audit_store=self.audit_store,
            allow_fallback=False,
        )

    def initial_state(self, incident_id: str) -> IncidentAgentState:
        return {
            "messages": [HumanMessage(content="Investigate the current incident.")],
            "incident_id": incident_id,
            "status": "investigating",
            "phase": "agent_loop",
            "alert": {"service": "order-service"},
            "current_round": 1,
            "max_rounds": 8,
            "hypotheses": [],
            "evidence": [],
            "retrieved_cases": [],
            "loaded_skill_names": [],
            "model_profile": "test",
            "model_call_count": 0,
            "tool_call_count": 0,
            "fallback_used": False,
            "report": None,
        }

    def guard(
        self,
        state: IncidentAgentState,
        *,
        cause_code: str,
        evidence_ids: list[str],
    ) -> GuardDecision:
        proposal = RootCauseProposal(
            root_service="payment-service",
            cause_code=cause_code,
            evidence_ids=evidence_ids,
            confidence=0.8,
            next_action="finish",
        )
        return can_generate_guarded_report(
            state,
            proposal,
            self.skills.policies_by_cause_code,
        )

    def endless_tool_model(self) -> ScriptedChatModel:
        return self.fake_model(
            [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "search_logs",
                        "args": {
                            "incident_id": "inc-4",
                            "service": "order-service",
                            "keyword": f"timeout-{index}",
                            "limit": 10,
                        },
                        "id": f"call-{index}",
                        "type": "tool_call",
                    }],
                )
                for index in range(20)
            ]
        )
```

The async `agent_harness` fixture opens `AgentCheckpointRuntime(tmp_path / "graph.db")`, constructs `EvidenceRecorder` and `build_agent_tools`, validates `SkillRuntime(Path("skills"), investigation_audit_store)`, yields the `AgentHarness`, and lets the async context close the SQLite connection.

```python
# tests/agent/test_llm_graph.py
from langchain_core.messages import AIMessage


async def test_llm_agent_executes_model_selected_tool_not_fixed_strategy(agent_harness) -> None:
    fake = agent_harness.fake_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_recent_deployments",
                        "args": {"incident_id": "inc-1", "service": "payment-service", "limit": 5},
                        "id": "model-call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Current evidence is insufficient; inspect the deployment."),
        ]
    )
    graph = agent_harness.build(model=fake)
    result = await graph.ainvoke(
        agent_harness.initial_state("inc-1"),
        {"configurable": {"thread_id": "inc-1"}},
    )
    assert result["tool_call_count"] == 1
    assert result["evidence"][0].source_tool == "list_recent_deployments"
    assert result["model_call_count"] == 2


async def test_historical_case_cannot_pass_current_evidence_gate(agent_harness) -> None:
    state = agent_harness.initial_state("inc-2")
    state["retrieved_cases"] = [
        {"id": "case-1", "root_cause": "bad_deployment", "status": "human_verified"}
    ]
    decision = agent_harness.guard(state, cause_code="bad_deployment", evidence_ids=[])
    assert decision.allowed is False
    assert decision.reason == "current_incident_evidence_required"


async def test_model_generated_unknown_evidence_id_is_rejected(agent_harness) -> None:
    state = agent_harness.initial_state("inc-3")
    decision = agent_harness.guard(
        state,
        cause_code="payment_latency_spike",
        evidence_ids=["ev-invented"],
    )
    assert decision.allowed is False
    assert decision.reason == "unknown_evidence_id"


async def test_model_and_tool_limits_stop_graph_with_explicit_code(agent_harness) -> None:
    graph = agent_harness.build(model=agent_harness.endless_tool_model())
    result = await graph.ainvoke(
        agent_harness.initial_state("inc-4"),
        {"configurable": {"thread_id": "inc-4"}},
    )
    assert result["model_call_count"] <= 12
    assert result["tool_call_count"] <= 12
    assert result["last_error_code"] == "budget_exhausted"
```

- [ ] **Step 2: Run graph tests and confirm no LLM graph exists**

Run: `uv run pytest tests/agent/test_llm_graph.py -q`

Expected: FAIL with missing graph and middleware modules.

- [ ] **Step 3: Implement stable prompts and structured repair**

`SYSTEM_PROMPT` must explicitly state:

```text
You investigate only the current incident.
Choose only registered read-only observability tools.
Read a relevant Skill before relying on its evidence policy.
Historical cases are priors, never current proof.
Never invent tool results or Evidence IDs.
When evidence is insufficient or contradictory, say so and stop safely.
Do not request writes, Shell, rollback, restart, or configuration mutation.
```

Build current context from bounded summaries: alert, up to 8 hypotheses, up to 12 Evidence summaries, verified historical cases, loaded Skill names, round count, and remaining model/tool budgets. Do not serialize raw API keys, Authorization headers, full unbounded logs, or hidden reasoning.

Use `RootCauseProposal` through LangChain `ToolStrategy`, followed by Pydantic validation. Permit one repair message containing only the validation errors. A second invalid response sets `last_error_code="model_output_invalid"` and `status="needs_more_evidence"`.

- [ ] **Step 4: Compose only public LangChain/LangGraph APIs**

Build with:

```python
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy

agent = create_agent(
    model=model,
    tools=agent_tools,
    system_prompt=SYSTEM_PROMPT,
    middleware=[
        *skill_runtime.middleware(),
        audit_middleware,
        ModelCallLimitMiddleware(thread_limit=12, exit_behavior="error"),
        ToolCallLimitMiddleware(thread_limit=12, exit_behavior="error"),
        report_gate_middleware,
    ],
    response_format=ToolStrategy(RootCauseProposal),
    state_schema=IncidentAgentState,
    context_schema=InvestigationContext,
    checkpointer=checkpointer,
    name="incidentlens-investigator",
)
```

Add `TransportOnlyModelFallbackMiddleware` only when `fallback_profiles` is non-empty. Do not add `ModelRetryMiddleware`. Ensure the live-test builder accepts `allow_fallback=False`.

After each tool result, append only the adapter-created Evidence. Run `evidence_rules.py` to update hypotheses; never use it to choose the next tool. Gate report generation using the selected Skill's `EvidencePolicy`, independent source types, no direct contradiction, current incident ownership, and valid Evidence IDs.

The evidence middleware converts each Task 4 `ToolMessage.artifact` into a LangGraph state update:

```python
response = await handler(request)
envelope = AgentToolEnvelope.model_validate(response.artifact)
return Command(
    update={
        "messages": [response],
        "evidence": [envelope.evidence],
        "tool_call_count": 1,
    }
)
```

If `handler` raises Pydantic validation, the same middleware builds one stable `invalid_arguments` Evidence from `request.tool_call`, returns an error `ToolMessage` through `Command`, and permits one model repair. It never invokes the repository for that call.

- [ ] **Step 5: Run graph, evidence, and report tests**

Run:

```bash
uv run pytest tests/agent/test_llm_graph.py tests/agent/test_evidence_rules.py tests/agent/test_tool_adapter.py -q
```

Expected: PASS; fake models are injected explicitly and no network call occurs.

- [ ] **Step 6: Commit the real Agent graph**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent/prompts.py apps/control-plane/src/incidentlens_control_plane/agent/middleware.py apps/control-plane/src/incidentlens_control_plane/agent/graph.py apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py apps/control-plane/src/incidentlens_control_plane/agent/reporting.py tests/support tests/agent/conftest.py tests/agent/test_llm_graph.py
git commit -m "feat: build bounded evidence-driven llm agent"
```

### Task 7: Migrate the Engine, Keep an Explicit Baseline, and Remove Dual State Writes

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/baseline.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent/factory.py`
- Create: `tests/agent/test_runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/engine.py`
- Modify: `tests/agent/test_investigation_engine.py`

**Interfaces:**
- Consumes: compiled graph, checkpoint runtime, telemetry/case repositories, Skill/runtime/model factories.
- Produces: async protocol `start(alert)`, `run_round(incident_id)`, `resume(incident_id)`, `load(incident_id)`, `LLMInvestigationEngine`, unchanged synchronous `DeterministicInvestigationEngine`, `AsyncBaselineAdapter`, and `build_investigation_engine(*, mode: RuntimeMode, telemetry_repo: TelemetryRepository, toolkit: ReadOnlyToolkit, case_repository: CaseRepository | None, audit_store: InvestigationAuditStore, checkpointer: BaseCheckpointSaver | None, skill_runtime: SkillRuntime | None, model_registry: ModelRegistry | None) -> InvestigationEngineProtocol`.

- [ ] **Step 1: Write mode, missing-config, and terminal-runtime tests**

```python
# tests/agent/test_runtime.py
from pathlib import Path
from types import SimpleNamespace

import pytest

from incidentlens_control_plane.agent.factory import build_investigation_engine
from incidentlens_control_plane.agent.runtime import LLMInvestigationEngine
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.llm.registry import ModelIdentity


def test_llm_mode_requires_registry_and_never_auto_selects_baseline(
    telemetry_repo,
    toolkit,
    investigation_audit_store,
) -> None:
    with pytest.raises(ValueError, match="ModelRegistry"):
        build_investigation_engine(
            mode=RuntimeMode.LLM_AGENT,
            model_registry=None,
            telemetry_repo=telemetry_repo,
            toolkit=toolkit,
            case_repository=None,
            audit_store=investigation_audit_store,
            checkpointer=object(),
            skill_runtime=object(),
        )


def test_baseline_mode_does_not_construct_model_registry(
    telemetry_repo,
    toolkit,
    investigation_audit_store,
) -> None:
    engine = build_investigation_engine(
        mode=RuntimeMode.DETERMINISTIC_BASELINE,
        model_registry=None,
        telemetry_repo=telemetry_repo,
        toolkit=toolkit,
        case_repository=None,
        audit_store=investigation_audit_store,
        checkpointer=None,
        skill_runtime=None,
    )
    assert engine.mode is RuntimeMode.DETERMINISTIC_BASELINE


async def test_terminal_incident_does_not_restart(investigation_audit_store) -> None:
    terminal = {
        "messages": [],
        "incident_id": "inc-terminal",
        "status": "needs_more_evidence",
        "phase": "finished",
        "alert": {"service": "order-service"},
        "current_round": 2,
        "max_rounds": 8,
        "hypotheses": [],
        "evidence": [],
        "retrieved_cases": [],
        "loaded_skill_names": [],
        "model_profile": "test",
        "model_call_count": 1,
        "tool_call_count": 0,
        "fallback_used": False,
        "report": None,
    }

    class StubGraph:
        invocations = 0

        async def aget_state(self, config):
            return SimpleNamespace(values=terminal)

        async def ainvoke(self, *args, **kwargs):
            self.invocations += 1
            raise AssertionError("terminal thread was restarted")

    graph = StubGraph()
    engine = LLMInvestigationEngine(
        graph=graph,
        audit_store=investigation_audit_store,
        model_identity=ModelIdentity("test", "test-model", "example.test"),
        case_repository=None,
        total_timeout_seconds=1200,
    )
    resumed = await engine.resume("inc-terminal")
    assert resumed.status.value == "needs_more_evidence"
    assert graph.invocations == 0


def test_llm_engine_source_has_no_fixed_tool_strategy() -> None:
    source = Path(
        "apps/control-plane/src/incidentlens_control_plane/agent/runtime.py"
    ).read_text(encoding="utf-8")
    assert "_TOOL_STRATEGY" not in source
```

- [ ] **Step 2: Run runtime tests and confirm missing factory**

Run: `uv run pytest tests/agent/test_runtime.py -q`

Expected: FAIL with missing runtime/factory modules.

- [ ] **Step 3: Move the deterministic engine without changing its behavior**

Move the current `InvestigationEngine` implementation and `_TOOL_STRATEGY` to `baseline.py`, rename the class `DeterministicInvestigationEngine`, add `mode = RuntimeMode.DETERMINISTIC_BASELINE`, and keep its synchronous `start`, existing async `run_round`/`resume`, and custom `CheckpointStore` unchanged for baseline regression tests. Change `tests/agent/test_investigation_engine.py` imports to the explicit baseline class.

Make `engine.py` a compatibility module:

```python
from .baseline import DeterministicInvestigationEngine
from .runtime import LLMInvestigationEngine

# Temporary compatibility for main.py until Task 9 installs the explicit factory.
InvestigationEngine = DeterministicInvestigationEngine

__all__ = [
    "DeterministicInvestigationEngine",
    "InvestigationEngine",
    "LLMInvestigationEngine",
]
```

- [ ] **Step 4: Implement the async LLM runtime facade**

Use exact async signatures:

```python
class InvestigationEngineProtocol(Protocol):
    mode: RuntimeMode
    audit_store: InvestigationAuditStore

    async def start(self, alert: dict[str, Any]) -> InvestigationState: ...
    async def run_round(self, incident_id: str) -> InvestigationState: ...
    async def resume(self, incident_id: str) -> InvestigationState | None: ...
    async def load(self, incident_id: str) -> InvestigationState | None: ...
```

`LLMInvestigationEngine.start` creates the initial state, retrieves only verified historical cases, invokes the graph with `thread_id=incident_id`, and projects the saved state. `run_round` resumes the same thread for one bounded Agent invocation. `resume` loads the latest LangGraph state and continues only if non-terminal. It never calls custom `CheckpointStore.save`.

Its constructor is:

```python
def __init__(
    self,
    *,
    graph: Any,
    audit_store: InvestigationAuditStore,
    model_identity: ModelIdentity,
    case_repository: CaseRepository | None,
    total_timeout_seconds: float = 1200,
) -> None:
```

`start` generates the incident ID, validates the alert, queries `case_repository` when present, filters the returned cases to `human_verified`, builds the initial TypedDict, and invokes the graph. Those retrieved cases are saved in the LangGraph state; the runtime never reads a second checkpoint source.

Wrap the baseline for the shared async API:

```python
class AsyncBaselineAdapter:
    mode = RuntimeMode.DETERMINISTIC_BASELINE

    def __init__(self, delegate: DeterministicInvestigationEngine) -> None:
        self._delegate = delegate
        self.audit_store = delegate.audit_store

    async def start(self, alert: dict[str, Any]) -> InvestigationState:
        return self._delegate.start(alert)

    async def run_round(self, incident_id: str) -> InvestigationState:
        return await self._delegate.run_round(incident_id)

    async def resume(self, incident_id: str) -> InvestigationState | None:
        return await self._delegate.resume(incident_id)

    async def load(self, incident_id: str) -> InvestigationState | None:
        return self._delegate.checkpoint_store.load(incident_id)
```

`build_investigation_engine` returns this adapter for the explicit baseline mode and branches only on `RuntimeMode`. It does not inspect whether an API key exists.

- [ ] **Step 5: Run LLM runtime and baseline regression tests**

Run:

```bash
uv run pytest tests/agent/test_runtime.py tests/agent/test_investigation_engine.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the runtime migration**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent tests/agent/test_runtime.py tests/agent/test_investigation_engine.py
git commit -m "refactor: migrate investigations to explicit agent runtimes"
```

### Task 8: Add Recovery, Total Timeout, Error Taxonomy, and Non-Duplicating Resume

**Files:**
- Create: `tests/agent/test_recovery.py`
- Modify: `tests/agent/conftest.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/middleware.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/checkpoint.py`

**Interfaces:**
- Consumes: same `incident_id` graph thread, normalized tool call key, audit store.
- Produces: recoverable `model_timeout`, `model_unavailable`, `budget_exhausted`, `model_output_invalid`, `skill_load_failed`, and `checkpoint_corrupt` outcomes.

- [ ] **Step 1: Write the real SQLite interrupt/resume test**

Extend `tests/agent/conftest.py` with:

```python
@dataclass
class RecoveryRunResult:
    state: InvestigationState
    tool_executions: int


class RecoveryHarness:
    def __init__(
        self,
        *,
        engine: LLMInvestigationEngine,
        counted_toolkit: CountingToolkit,
        checkpoint_runtime: AgentCheckpointRuntime,
        scripted_model: InterruptibleScriptedChatModel,
    ) -> None:
        self.engine = engine
        self._toolkit = counted_toolkit
        self._checkpoints = checkpoint_runtime
        self._model = scripted_model

    @property
    def model_invocations(self) -> int:
        return self._model.invocations

    async def run_until_after_tool(
        self,
        *,
        incident_id: str,
        tool_name: str,
    ) -> RecoveryRunResult:
        self._model.interrupt_before_second_model_call = True
        await self.engine.start(
            {"incident_id": incident_id, "service": "order-service"}
        )
        state = await self.engine.load(incident_id)
        return RecoveryRunResult(state, self._toolkit.count(tool_name))

    async def resume(self, incident_id: str) -> RecoveryRunResult:
        self._model.interrupt_before_second_model_call = False
        state = await self.engine.resume(incident_id)
        assert state is not None
        return RecoveryRunResult(state, self._toolkit.total_count)

    async def run_with_model_timeout(self, incident_id: str) -> RecoveryRunResult:
        self._model.timeout_before_second_model_call = True
        await self.engine.start(
            {"incident_id": incident_id, "service": "order-service"}
        )
        state = await self.engine.load(incident_id)
        return RecoveryRunResult(state, self._toolkit.total_count)

    async def resume_with_healthy_model(self, incident_id: str) -> RecoveryRunResult:
        self._model.timeout_before_second_model_call = False
        return await self.resume(incident_id)

    async def insert_corrupt_checkpoint(self, incident_id: str) -> None:
        await self._checkpoints.saver.conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint = ?
            WHERE thread_id = ?
              AND checkpoint_id = (
                SELECT checkpoint_id
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY checkpoint_id DESC
                LIMIT 1
              )
            """,
            (b"{invalid", incident_id, incident_id),
        )
        await self._checkpoints.saver.conn.commit()
```

`CountingToolkit` delegates all seven methods to the real `ReadOnlyToolkit` and increments a dictionary before each delegation. `InterruptibleScriptedChatModel` extends `ScriptedChatModel`, emits one `search_logs` tool call first, then either calls LangGraph `interrupt({"reason": "test_after_tool"})`, raises `TimeoutError`, or emits a valid `RootCauseProposal` tool call. The async `recovery_harness` fixture opens a real `AgentCheckpointRuntime`, seeds one timeout log, builds the real Agent graph and runtime, and yields this class. Checkpoint corruption is confined to this test harness and the temporary SQLite file; production code receives only the resulting deserialization failure.

```python
# tests/agent/test_recovery.py
async def test_resume_does_not_repeat_successful_tool_or_change_evidence_id(
    recovery_harness,
) -> None:
    first = await recovery_harness.run_until_after_tool(
        incident_id="inc-recover",
        tool_name="search_logs",
    )
    assert first.tool_executions == 1
    evidence_id = first.state.evidence[0].id

    resumed = await recovery_harness.resume("inc-recover")

    assert resumed.tool_executions == 1
    assert resumed.state.evidence[0].id == evidence_id
    assert resumed.state.last_checkpoint_id != first.state.last_checkpoint_id


async def test_model_timeout_keeps_completed_evidence_and_is_resumable(
    recovery_harness,
) -> None:
    result = await recovery_harness.run_with_model_timeout("inc-timeout")
    assert result.state.last_error_code == "model_timeout"
    assert result.state.evidence
    resumed = await recovery_harness.resume_with_healthy_model("inc-timeout")
    assert resumed.state.evidence[0].id == result.state.evidence[0].id


async def test_corrupt_checkpoint_never_restarts_from_empty_state(recovery_harness) -> None:
    await recovery_harness.insert_corrupt_checkpoint("inc-corrupt")
    with pytest.raises(CheckpointCorruptError):
        await recovery_harness.engine.resume("inc-corrupt")
    assert recovery_harness.model_invocations == 0
```

- [ ] **Step 2: Run recovery tests and confirm resume behavior is incomplete**

Run: `uv run pytest tests/agent/test_recovery.py -q`

Expected: FAIL on missing interrupt hooks/error taxonomy.

- [ ] **Step 3: Implement bounded total timeout and stable resume**

Wrap one investigation invocation with:

```python
try:
    async with asyncio.timeout(self._total_timeout_seconds):
        result = await self._graph.ainvoke(inputs, config, context=context)
except TimeoutError:
    await self._record_recoverable_failure(
        incident_id,
        code="model_timeout",
        safe_details={"profile": self._model_identity.profile},
    )
    return await self._load_projected_state(incident_id)
```

Use 1200 seconds in production and an injected short duration in tests. Store no secrets or complete Authorization/request content. On resume, derive normalized tool call keys from the saved state/audit; return the previously recorded envelope without querying the repository again.

If the latest LangGraph snapshot contains an interrupt, `resume` invokes the graph with `Command(resume=True)` and the same `thread_id`; otherwise it invokes the normal bounded continuation input. `load` only calls `graph.aget_state` plus `project_investigation_state` and never advances the graph.

Map only explicit categories:

```text
model_timeout
model_unavailable
budget_exhausted
model_output_invalid
skill_load_failed
checkpoint_corrupt
```

Do not map these failures to a deterministic conclusion or fake model.

- [ ] **Step 4: Run recovery, graph, and tool suites**

Run:

```bash
uv run pytest tests/agent/test_recovery.py tests/agent/test_llm_graph.py tests/agent/test_tool_adapter.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit recovery behavior**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent/runtime.py apps/control-plane/src/incidentlens_control_plane/agent/middleware.py apps/control-plane/src/incidentlens_control_plane/agent/checkpoint.py tests/agent/conftest.py tests/agent/test_recovery.py
git commit -m "feat: add recoverable llm investigation execution"
```

### Task 9: Wire FastAPI Lifespan, Async Routes, Safe Audit, and SSE

**Files:**
- Create: `tests/web/conftest.py`
- Create: `tests/web/test_investigation_agent_api.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events.py`
- Modify: `tests/web/test_events.py`

**Interfaces:**
- Consumes: `AgentCheckpointRuntime`, runtime factory, async engine protocol, `EventBus`.
- Produces: async API responses containing `mode`, `model_profile`, `last_error_code`, `last_checkpoint_id`; safe SSE types `model_called`, `skill_loaded`, `tool_called`, `evidence_recorded`, `state_changed`, `report_ready`.

- [ ] **Step 1: Write failing async API and secret-redaction tests**

```python
# tests/web/conftest.py
from contextlib import asynccontextmanager

import httpx
import pytest
from incidentlens_contracts.models import InvestigationStatus

from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.llm.config import RuntimeMode
from incidentlens_control_plane.main import create_app


class FakeAsyncEngine:
    mode = RuntimeMode.LLM_AGENT

    def __init__(self, audit_store) -> None:
        self.audit_store = audit_store
        self.state = InvestigationState(
            incident_id="inc-api",
            status=InvestigationStatus.INVESTIGATING,
            alert={"service": "order-service"},
            phase="agent_loop",
            model_profile="deepseek",
            last_checkpoint_id="checkpoint-1",
        )

    async def start(self, alert):
        self.state.alert = alert
        return self.state

    async def run_round(self, incident_id):
        return self.state

    async def resume(self, incident_id):
        return self.state


@pytest.fixture
def fake_agent_engine(investigation_audit_store) -> FakeAsyncEngine:
    return FakeAsyncEngine(investigation_audit_store)


@pytest.fixture
async def agent_api_client(fake_agent_engine):
    app = create_app(engine_override=fake_agent_engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
```

```python
# tests/web/test_investigation_agent_api.py
import json

from incidentlens_control_plane.agent.middleware import redact_sensitive_payload


async def test_start_awaits_engine_and_exposes_runtime_identity(
    agent_api_client,
) -> None:
    response = await agent_api_client.post(
        "/api/investigations/start",
        json={"service": "order-service", "error_rate": 0.17},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["mode"] == "llm_agent"
    assert body["model_profile"] == "deepseek"
    assert body["last_checkpoint_id"]


def test_sse_and_audit_never_expose_model_secret() -> None:
    secret = "super-secret-key"
    safe = redact_sensitive_payload(
        {
            "api_key": secret,
            "Authorization": f"Bearer {secret}",
            "nested": {"token": secret, "model": "deepseek-chat"},
        },
        secret_values={secret},
    )
    payload = json.dumps(safe)
    assert secret not in payload
    assert "Bearer" not in payload
    assert safe["nested"]["model"] == "deepseek-chat"


async def test_model_timeout_is_not_returned_as_success(
    agent_api_client,
    fake_agent_engine,
) -> None:
    fake_agent_engine.state.last_error_code = "model_timeout"
    response = await agent_api_client.post("/api/investigations/inc-api/round")
    assert response.status_code == 200
    assert response.json()["last_error_code"] == "model_timeout"
    assert response.json()["status"] != "report_ready"
```

- [ ] **Step 2: Run API tests and confirm synchronous start mismatch**

Run: `uv run pytest tests/web/test_investigation_agent_api.py -q`

Expected: FAIL because the route calls `_engine.start` without `await` and omits new fields.

- [ ] **Step 3: Move resource ownership into FastAPI lifespan**

Refactor `main.py` to `create_app(*, engine_override: InvestigationEngineProtocol | None = None) -> FastAPI` and async lifespan. When `engine_override` is supplied, install only that injected engine and yield without reading model config; this branch exists for tests and has no environment-based fake fallback. The production module calls `app = create_app()` with no override:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine_override is not None:
        set_investigation_engine(engine_override)
        yield
        return
    mode = RuntimeMode(os.environ.get("INCIDENTLENS_AGENT_MODE", "llm_agent"))
    db_url = os.environ.get("TELEMETRY_DB_URL", "sqlite:///control_plane.db")
    db_engine = create_engine(db_url)
    telemetry_repo = TelemetryRepository(db_engine)
    toolkit = ReadOnlyToolkit(telemetry_repo)
    case_repository = CaseRepository(db_engine)
    audit_store = InvestigationAuditStore(db_engine)
    checkpoint_path = Path(
        os.environ.get("INCIDENTLENS_CHECKPOINT_DB", "agent_checkpoints.db")
    )
    if mode is RuntimeMode.LLM_AGENT:
        config_path = Path(
            os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml")
        )
        models = load_models_config(config_path, os.environ)
        registry = ModelRegistry(models, os.environ)
        registry.get()  # startup validation and construction; no provider request
        skill_runtime = SkillRuntime(Path("skills"), audit_store)
        skill_runtime.validate()
        async with AgentCheckpointRuntime(checkpoint_path) as checkpoints:
            runtime = build_investigation_engine(
                mode=mode,
                telemetry_repo=telemetry_repo,
                toolkit=toolkit,
                case_repository=case_repository,
                audit_store=audit_store,
                model_registry=registry,
                checkpointer=checkpoints.saver,
                skill_runtime=skill_runtime,
            )
            set_investigation_engine(runtime)
            yield
    else:
        set_investigation_engine(
            build_investigation_engine(
                mode=mode,
                telemetry_repo=telemetry_repo,
                toolkit=toolkit,
                case_repository=case_repository,
                audit_store=audit_store,
                model_registry=None,
                checkpointer=None,
                skill_runtime=None,
            )
        )
        yield
```

`app = create_app()` remains the Uvicorn entry. Missing/empty keys in default `llm_agent` mode fail lifespan startup; tests use the explicit baseline default in `tests/conftest.py` or inject a fake runtime into `create_app`.

- [ ] **Step 4: Await routes and publish only safe normalized events**

Change `state = _engine.start(alert)` to `state = await _engine.start(alert)`. Extend `InvestigationStateResponse` with:

```python
mode: str
model_profile: str
model_call_count: int
tool_call_count: int
loaded_skill_names: list[str]
fallback_used: bool
last_error_code: str | None
last_checkpoint_id: str | None
```

Publish `model_called` with profile/model/endpoint host/duration/token counts only; `skill_loaded` with Skill name/path only; and `tool_called` with normalized bounded arguments. Centralize redaction for keys matching `authorization`, `api_key`, `token`, `secret`, and the resolved secret values before audit/SSE serialization.

- [ ] **Step 5: Run API, SSE, and existing route tests**

Run:

```bash
uv run pytest tests/web/test_investigation_agent_api.py tests/web/test_events.py tests/scenarios/test_api.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the application integration**

```bash
git add apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/routes/investigations.py apps/control-plane/src/incidentlens_control_plane/events.py tests/web/conftest.py tests/web/test_investigation_agent_api.py tests/web/test_events.py
git commit -m "feat: expose async llm investigations through api"
```

### Task 10: Configure Compose and DemoRunner for Real Long-Running LLM Investigations

**Files:**
- Modify: `infra/compose/Dockerfile`
- Modify: `infra/compose/compose.yaml`
- Modify: `packages/demo/src/incidentlens_demo/runner.py`
- Modify: `tests/demo/test_runner.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_compose_flow.py`
- Modify: `tests/integration/test_scenario_acceptance.py`
- Create: `tests/integration/test_live_agent_compose.py`

**Interfaces:**
- Consumes: configured control-plane API, `DEEPSEEK_API_KEY`/`GLM_API_KEY`, Agent response identity.
- Produces: deterministic Compose mode for ordinary integration tests and explicit live LLM Compose mode with 1200-second client budget.

- [ ] **Step 1: Write failing DemoRunner timeout and live contract tests**

```python
# tests/demo/test_runner.py
def test_llm_runner_uses_twenty_minute_investigation_timeout() -> None:
    runner = DemoRunner(
        control_plane_url="http://control-plane",
        gateway_url="http://gateway",
        mode="llm_agent",
    )
    assert runner.investigation_timeout_seconds == 1200


async def test_live_contract_requires_model_skill_tool_and_current_evidence(mock_client) -> None:
    runner = DemoRunner(
        control_plane_url="http://control-plane",
        gateway_url="http://gateway",
        mode="llm_agent",
    )
    runner._client = mock_client
    result = await runner.run("payment_delay")
    assert result.model_profile == "deepseek"
    assert result.loaded_skill_names == ["downstream-timeout"]
    assert result.model_call_count > 0
    assert result.tool_call_count > 0
    assert result.report["evidence_ids"]
```

Create a marked Compose test:

```python
# tests/integration/test_live_agent_compose.py
@pytest.mark.integration
@pytest.mark.live_llm
async def test_real_model_completes_payment_delay_investigation(live_compose_urls) -> None:
    runner = DemoRunner(
        control_plane_url=live_compose_urls["control_plane_url"],
        gateway_url=live_compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
        mode="llm_agent",
    )
    result = await runner.run("payment_delay")
    assert result.status == "passed", result.failure_message
    assert result.model_call_count > 0
    assert result.tool_call_count > 0
    assert "downstream-timeout" in result.loaded_skill_names
    assert result.report["root_service"] == "payment-service"
    assert result.fallback_used is False
    assert all(
        evidence_id.startswith("ev-")
        for evidence_id in result.report["evidence_ids"]
    )
```

- [ ] **Step 2: Run focused tests and confirm missing live metadata**

Run: `uv run pytest tests/demo/test_runner.py -q`

Expected: FAIL on missing `mode`, 1200-second timeout, and live result fields.

- [ ] **Step 3: Package configuration and Skills into Compose safely**

Add to the Dockerfile build/runtime:

```dockerfile
COPY config/ config/
COPY skills/ skills/
```

Configure control-plane:

```yaml
environment:
  - INCIDENTLENS_AGENT_MODE=${INCIDENTLENS_AGENT_MODE:-llm_agent}
  - INCIDENTLENS_MODELS_CONFIG=/app/config/models.yaml
  - INCIDENTLENS_LLM_ACTIVE_MODEL=${INCIDENTLENS_LLM_ACTIVE_MODEL:-deepseek}
  - INCIDENTLENS_CHECKPOINT_DB=/data/agent_checkpoints.db
  - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}
  - GLM_API_KEY=${GLM_API_KEY:-}
volumes:
  - control-plane-data:/data
  - ../../config:/app/config:ro
  - ../../skills:/app/skills:ro
```

Ordinary integration fixture subprocesses must pass `INCIDENTLENS_AGENT_MODE=deterministic_baseline` explicitly in their `env`. The live fixture requires a complete active profile/key, passes `llm_agent`, uses a 600-second Compose build/start timeout, and does not skip partial/empty configuration.

Implement the live fixture in `tests/integration/conftest.py` using the same production config loader:

```python
import os
from pathlib import Path

from incidentlens_control_plane.llm.config import (
    load_models_config,
    resolve_model_profile,
)


@pytest.fixture(scope="session")
def live_compose_urls() -> Generator[dict[str, str], None, None]:
    config_path = Path(
        os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml")
    )
    config = load_models_config(config_path, os.environ)
    key_name = config.models[config.active_model].api_key_env
    if key_name not in os.environ:
        pytest.skip(f"missing {key_name}")
    if not os.environ[key_name].strip():
        pytest.fail(f"{key_name} exists but is empty")
    resolve_model_profile(config, config.active_model, os.environ)
    if not _docker_available():
        pytest.fail("Docker is required for live Agent Compose verification")

    compose_env = {
        **os.environ,
        "INCIDENTLENS_AGENT_MODE": "llm_agent",
        "INCIDENTLENS_LLM_ACTIVE_MODEL": config.active_model,
    }
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "--build", "-d"],
        check=True,
        timeout=600,
        env=compose_env,
    )
    if not _wait_for_health(CONTROL_PLANE_URL, timeout=120):
        subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v"],
            check=False,
            timeout=120,
            env=compose_env,
        )
        pytest.fail("live control-plane did not become healthy")
    try:
        yield {
            "control_plane_url": CONTROL_PLANE_URL,
            "gateway_url": GATEWAY_URL,
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v"],
            check=True,
            timeout=120,
            env=compose_env,
        )
```

- [ ] **Step 4: Extend DemoRunner without shortening model timeouts**

Use `httpx.Timeout(connect=15, read=1200, write=30, pool=15)` for investigation start/round/resume requests. Keep traffic and runtime polling on shorter clients. Add these fields at the end of `DemoRunResult` so existing failure constructors remain valid:

```python
mode: str = "llm_agent"
model_profile: str = ""
model_call_count: int = 0
tool_call_count: int = 0
loaded_skill_names: list[str] = field(default_factory=list)
fallback_used: bool = False
```

Add `mode: str = "llm_agent"` to `DemoRunner.__init__`; validate it against the two `RuntimeMode` values and use it as an expected-response assertion, never as an API-key-based mode switch. Update every existing deterministic unit/integration runner construction to pass `mode="deterministic_baseline"`. The production CLI default remains `llm_agent`.

For `mode="llm_agent"`, fail the run unless API/audit evidence proves:

```text
model_call_count > 0
tool_call_count > 0
loaded_skill_names is non-empty
every report Evidence ID belongs to the current incident
fallback_used is false for the required live acceptance
```

- [ ] **Step 5: Run deterministic Demo and Compose regressions**

Run: `uv run pytest tests/demo/test_runner.py tests/demo/test_run_demo_cli.py -q`

Expected: PASS.

Run:

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline uv run pytest tests/integration/test_compose_flow.py tests/integration/test_scenario_acceptance.py -m integration -q
```

Expected: PASS without any model key.

- [ ] **Step 6: Commit Compose and Demo wiring**

```bash
git add infra/compose/Dockerfile infra/compose/compose.yaml packages/demo/src/incidentlens_demo/runner.py tests/demo/test_runner.py tests/integration/conftest.py tests/integration/test_compose_flow.py tests/integration/test_scenario_acceptance.py tests/integration/test_live_agent_compose.py
git commit -m "feat: run configured llm agents in compose"
```

### Task 11: Verify the User Configuration and One Real End-to-End Agent Scenario

**Files:**
- Modify only if a test exposes a defect in files from Tasks 1-10.
- Record command output in: `docs/phase-3-live-verification.md`

**Interfaces:**
- Consumes: the user's active `config/models.yaml`/override, environment key, production `ModelRegistry`, live Compose fixture.
- Produces: redacted evidence that the selected endpoint/model accepted a real tool call and completed one real Agent investigation.

- [ ] **Step 1: Validate configuration without printing the secret**

Run:

```bash
uv run python -c "import os; from pathlib import Path; from incidentlens_control_plane.llm.config import load_models_config, resolve_model_profile; p=Path(os.environ.get('INCIDENTLENS_MODELS_CONFIG','config/models.yaml')); c=load_models_config(p,os.environ); r=resolve_model_profile(c,c.active_model,os.environ); print({'profile':r.name,'model':r.model,'endpoint':str(r.base_url),'key_present':bool(r.api_key.get_secret_value())})"
```

Expected: selected profile/model/endpoint and `key_present: True`; no key value in output. If the key is absent, stop live execution and report the exact pending status required by Global Constraints.

- [ ] **Step 2: Run the same-registry real canary**

Run:

```bash
uv run pytest tests/live_llm/test_model_contract.py -m live_llm -vv -s
```

Expected: PASS with a redacted profile/model/endpoint identity, unique nonce tool call, and `fallback_used=False`. Authentication, invalid URL, empty key, or missing tool call is a FAIL, not a skip.

- [ ] **Step 3: Run the required real Compose investigation**

Run:

```bash
uv run pytest tests/integration/test_live_agent_compose.py::test_real_model_completes_payment_delay_investigation -m "integration and live_llm" -vv -s
```

Expected within 1200 seconds: PASS with real model audit, `downstream-timeout` read, at least one model-selected observability tool, current incident Evidence, guarded `payment-service` report, and no fallback.

- [ ] **Step 4: Check logs and serialized outputs for secrets/fakes/fixed strategy**

Run:

```bash
rg -n "Fake(Chat)?Model|_TOOL_STRATEGY|DEEPSEEK_API_KEY=|GLM_API_KEY=|Authorization" apps/control-plane packages/demo tests/integration
```

Expected: `_TOOL_STRATEGY` appears only in the deterministic baseline and negative tests; no secret value or production fake-model branch appears.

Run:

```bash
docker compose -f infra/compose/compose.yaml logs control-plane | rg -n "api[_-]?key|authorization|bearer"
```

Expected: no secret-bearing log entry.

- [ ] **Step 5: Record redacted verification evidence**

Create `docs/phase-3-live-verification.md` only after both live commands pass. Use the title `# Phase 3 Live Verification`, then record the actual UTC test timestamp, `ModelIdentity.profile`, `ModelIdentity.model`, redacted `ModelIdentity.endpoint_host`, canary nonce-match result, fallback state, `payment_delay` scenario name, actual model/tool counts, `downstream-timeout`, `payment-service`, actual current-incident Evidence IDs, and secret-scan result. Copy values from the redacted test output; do not write example values or a passing record when either live command fails.

- [ ] **Step 6: Commit only after both live checks pass**

```bash
git add docs/phase-3-live-verification.md
git commit -m "test: verify live llm agent investigation"
```

### Task 12: Extend Live Evaluation to Five Scenarios and Finish Quality Gates

**Files:**
- Modify: `tests/integration/test_live_agent_compose.py`
- Modify: `packages/evaluation/src/incidentlens_evaluation/metrics.py`
- Modify: `tests/evaluation/test_metrics.py`
- Modify: `README.md`
- Modify: `docs/evaluation.md`
- Create: `docs/phase-3-live-evaluation.md`

**Interfaces:**
- Consumes: all five real scenarios and live Agent result metadata.
- Produces: root-service/type accuracy, evidence sufficiency, dangerous-action rate, model/tool call counts, duration, retries, token usage when provided, and failure analysis.

- [ ] **Step 1: Write failing five-scenario metric tests**

```python
# tests/evaluation/test_metrics.py
def test_agent_evaluation_tracks_live_agent_dimensions() -> None:
    metrics = AgentEvaluationMetrics.from_results(
        [
            {
                "scenario": "payment_delay",
                "passed": True,
                "root_service_correct": True,
                "cause_code_correct": True,
                "evidence_sufficient": True,
                "dangerous_actions": 0,
                "model_calls": 4,
                "tool_calls": 3,
                "duration_ms": 12500,
                "retries": 0,
                "token_usage": 1800,
            }
        ]
    )
    assert metrics.root_service_accuracy == 1.0
    assert metrics.cause_code_accuracy == 1.0
    assert metrics.evidence_sufficiency_rate == 1.0
    assert metrics.dangerous_action_rate == 0.0
    assert metrics.average_model_calls == 4
    assert metrics.average_tool_calls == 3
```

Parametrize the live test with:

```python
LIVE_SCENARIOS = [
    ("payment_delay", "payment-service", "payment_latency_spike", "downstream-timeout"),
    ("payment_error_rate", "payment-service", "payment_service_degradation", "downstream-error"),
    ("db_pool_exhaustion", "order-service", "database_connection_leak", "database-pool-exhaustion"),
    ("dependency_unavailable", "order-service", "network_partition", "dependency-unavailable"),
    ("deployment_regression", "payment-service", "bad_deployment", "deployment-regression"),
]
```

- [ ] **Step 2: Run metric tests and confirm missing dimensions**

Run: `uv run pytest tests/evaluation/test_metrics.py -q`

Expected: FAIL because current metrics do not contain the live Agent dimensions.

- [ ] **Step 3: Implement aggregate metrics and failure records**

Use zero-safe means and rates; retain per-scenario failure stage, error code, selected tools, loaded Skills, retries, and redacted model identity. Never persist a secret or hidden reasoning. Token usage may be `None` when the provider omits it and must not be fabricated.

- [ ] **Step 4: Run all five live scenarios**

Run:

```bash
uv run pytest tests/integration/test_live_agent_compose.py -m "integration and live_llm" -vv -s
```

Expected: five scenario results are recorded. A model miss remains a failed evaluation data point and must not be rewritten as a pass; debug and fix only deterministic implementation defects.

- [ ] **Step 5: Document exact operation and honest completion state**

Update `README.md` with:

```bash
cp .env.example .env
# Set exactly one active profile key, then:
docker compose --env-file .env -f infra/compose/compose.yaml up --build
uv run pytest tests/live_llm -m live_llm -vv
uv run pytest tests/integration/test_live_agent_compose.py -m "integration and live_llm" -vv
```

Document:

- config-only DeepSeek/GLM switching;
- missing/empty/invalid config behavior;
- 15-second connect, 300-second read, and 1200-second investigation timeouts;
- explicit baseline mode for comparison only;
- all five Skills and their policy gates;
- checkpoint/resume semantics;
- live tests' exact skip/fail rules;
- the required pending status when live verification is absent.

Write `docs/phase-3-live-evaluation.md` from actual run output with per-scenario pass/fail, evidence, call counts, latency, retries, token usage when available, and concrete failure analysis.

- [ ] **Step 6: Run the complete non-live quality gate**

Run:

```bash
uv run pytest -m "not live_llm and not integration" -q
uv run ruff check .
uv run mypy packages apps
```

Expected: all PASS.

- [ ] **Step 7: Run the deterministic Compose regression**

Run:

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline uv run pytest tests/integration/test_compose_flow.py tests/integration/test_scenario_acceptance.py -m integration -q
```

Expected: all existing five-scenario baseline acceptance tests PASS.

- [ ] **Step 8: Verify dependency and placeholder constraints**

Run:

```bash
uv run python -c "import importlib.metadata as m; assert m.version('langchain')=='1.3.14'; assert m.version('langgraph')=='1.2.9'; assert m.version('langchain-openai')=='1.4.1'; assert m.version('langgraph-checkpoint-sqlite')=='3.1.0'; assert m.version('deepagents')=='0.6.12'; assert m.version('pydantic')=='2.13.4'"
rg -n "LLMChain|ConversationChain|langchain_classic|create_react_agent|create_deep_agent|import openai|from openai" apps packages
```

Expected: version assertion succeeds and forbidden API search returns no matches.

- [ ] **Step 9: Commit documentation and evaluation**

```bash
git add README.md docs/evaluation.md docs/phase-3-live-evaluation.md packages/evaluation/src/incidentlens_evaluation/metrics.py tests/evaluation/test_metrics.py tests/integration/test_live_agent_compose.py
git commit -m "docs: record phase 3 live agent evaluation"
```

- [ ] **Step 10: Apply the completion wording gate**

If the canary and at least one real Compose scenario both passed, report Phase 3 complete with the verified profile/model/endpoint host and test commands.

If code gates passed but either live requirement did not pass, report exactly:

```text
Agent implementation complete; live provider verification pending.
```

Do not replace either outcome with a claim based only on fake-model, baseline, unit, or deterministic Compose tests.
