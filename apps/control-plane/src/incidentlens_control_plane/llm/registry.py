"""LangChain Model Registry for IncidentLens."""
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import ModelsConfig, resolve_model_profile


@dataclass(frozen=True)
class ModelIdentity:
    """Immutable identity metadata for a model profile."""
    profile: str
    model: str
    endpoint_host: str


class ModelRegistry:
    """Registry for creating LangChain chat models from config profiles."""

    def __init__(self, config: ModelsConfig, environ: Mapping[str, str]) -> None:
        self._config = config
        self._environ = environ

    @property
    def active_profile(self) -> str:
        """Return the name of the active model profile."""
        return self._config.active_model

    @property
    def fallback_profiles(self) -> tuple[str, ...]:
        """Return the names of fallback model profiles."""
        return tuple(self._config.fallback_models)

    def get(self, profile_name: str | None = None) -> BaseChatModel:
        """Create and return a LangChain chat model for the given profile.

        Args:
            profile_name: Profile name to use. Defaults to the active profile.

        Returns:
            A configured BaseChatModel instance.

        Raises:
            ValueError: If the profile name is unknown or the API key is missing.
        """
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
        """Return immutable identity metadata for a profile.

        Args:
            profile_name: Profile name to use. Defaults to the active profile.

        Returns:
            A ModelIdentity dataclass with profile, model, and endpoint_host.
        """
        name = profile_name or self.active_profile
        profile = self._config.models[name]
        return ModelIdentity(
            profile=name,
            model=profile.model,
            endpoint_host=urlsplit(str(profile.base_url)).netloc,
        )
