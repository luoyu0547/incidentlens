"""Embedding provider protocol and helpers for hybrid retrieval.

Provides the abstraction layer for semantic search. When the embedding
provider is unavailable, times out, or fails, the system degrades to
keyword-only search gracefully.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmbeddingUnavailableError(Exception):
    """Raised when the embedding provider is unavailable or times out."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Embedding unavailable: {reason}")


class EmbeddingDimensionError(Exception):
    """Raised when embedding vectors have mismatched dimensions."""


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingIdentity:
    """Identifies which embedding provider and model produced vectors."""

    provider: str
    model: str
    dimension: int


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers used in semantic search."""

    @property
    def identity(self) -> EmbeddingIdentity: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# ---------------------------------------------------------------------------
# Disabled provider (used when no embedding is configured)
# ---------------------------------------------------------------------------


class DisabledEmbeddingProvider:
    """Placeholder provider that raises on every call.

    Used when no embedding model is configured, ensuring the system
    degrades to keyword-only retrieval without crashing.
    """

    identity = EmbeddingIdentity(provider="disabled", model="", dimension=0)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("embedding_not_configured")

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailableError("embedding_not_configured")


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns a value in [-1, 1]. Raises :class:`EmbeddingDimensionError`
    if the vectors have different lengths.
    """
    if len(left) != len(right):
        raise EmbeddingDimensionError(f"{len(left)} != {len(right)}")
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(
        sum(x * x for x in right)
    )
    return (
        0.0
        if denominator == 0
        else sum(x * y for x, y in zip(left, right, strict=True)) / denominator
    )
