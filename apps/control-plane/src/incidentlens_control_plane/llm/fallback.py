"""Safe Fallback Middleware for LangChain models."""
from typing import Any, Iterator

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig


def is_retryable_transport_error(exc: BaseException) -> bool:
    """Check if an exception is a retryable transport error.

    Retries on:
    - httpx.TransportError subclasses (ConnectError, ReadTimeout, etc.)
    - HTTP status 429 (rate limit)
    - HTTP status 5xx (server errors)

    Does NOT retry on:
    - HTTP status 4xx (except 429)
    - Application-level errors (ValueError, etc.)

    Traverses __cause__ and __context__ chains to find transport errors.
    """
    for cause in _walk_exception_chain(exc):
        if isinstance(cause, httpx.TransportError):
            return True
        status_code = getattr(cause, "status_code", None)
        if isinstance(status_code, int) and (status_code == 429 or 500 <= status_code < 600):
            return True
    return False


def _walk_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Walk the exception chain via __cause__ and __context__."""
    visited = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        yield current
        next_exc = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        current = next_exc if isinstance(next_exc, BaseException) else None


class TransportOnlyModelFallbackMiddleware:
    """Middleware that falls back to another model only on transport errors.

    This middleware wraps a primary LangChain chat model and a fallback model.
    When the primary model fails with a retryable transport error, it automatically
    retries with the fallback model. Application errors (non-transport) are re-raised.
    """

    def __init__(self, primary: BaseChatModel, fallback: BaseChatModel) -> None:
        """Initialize the middleware.

        Args:
            primary: The primary chat model to use.
            fallback: The fallback chat model to use on transport errors.
        """
        self._primary = primary
        self._fallback = fallback

    def invoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke the primary model, falling back on transport errors."""
        try:
            return self._primary.invoke(input, config=config, **kwargs)
        except Exception as exc:
            if is_retryable_transport_error(exc):
                return self._fallback.invoke(input, config=config, **kwargs)
            raise

    async def ainvoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async invoke the primary model, falling back on transport errors."""
        try:
            return await self._primary.ainvoke(input, config=config, **kwargs)
        except Exception as exc:
            if is_retryable_transport_error(exc):
                return await self._fallback.ainvoke(input, config=config, **kwargs)
            raise
