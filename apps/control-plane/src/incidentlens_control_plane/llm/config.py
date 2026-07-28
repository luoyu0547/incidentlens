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
    profile_data = profile.model_dump()
    profile_data.pop("api_key_env", None)
    return ResolvedModelProfile(
        name=profile_name,
        api_key=SecretStr(secret),
        **profile_data,
    )
