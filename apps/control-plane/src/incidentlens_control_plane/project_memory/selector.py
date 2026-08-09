"""Memory selector for selecting relevant project memories.

Provides model-based and keyword-fallback selection of memories
relevant to the current incident context.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from incidentlens_control_plane.project_memory.domain import (
    MemoryCatalogEntry,
    MemoryQuery,
    MemorySelection,
)


def _normalize_unicode(text: str) -> str:
    """Normalize Unicode to NFKD form, fold accents, and lowercase."""
    # Normalize to NFKD form
    normalized = unicodedata.normalize("NFKD", text)
    # Fold combining characters (accents) by keeping only the base character
    folded = "".join(
        c for c in normalized
        if unicodedata.category(c) != "Mn"
    )
    return folded.lower()


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words."""
    normalized = _normalize_unicode(text)
    # Split on non-word characters, filter empty strings
    tokens = re.split(r"[^\w]+", normalized)
    # Filter out very short tokens (less than 2 characters)
    return [t for t in tokens if len(t) >= 2]


def _compute_keyword_scores(
    query_tokens: list[str],
    catalog: list[MemoryCatalogEntry],
) -> list[tuple[str, float]]:
    """Compute relevance scores for catalog entries based on keyword overlap."""
    scores: list[tuple[str, float]] = []
    query_set = set(query_tokens)

    for entry in catalog:
        # Tokenize the name and description
        name_tokens = set(_tokenize(entry.name))
        desc_tokens = set(_tokenize(entry.description))
        all_tokens = name_tokens | desc_tokens

        # Compute overlap score
        overlap = query_set & all_tokens
        if overlap:
            # Score is the number of overlapping tokens
            score = len(overlap) / len(query_set) if query_set else 0.0
            scores.append((entry.name, score))

    # Sort by score descending
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


async def select_memories(
    query: MemoryQuery,
    catalog: list[MemoryCatalogEntry],
    model: Any,
    limit: int = 5,
) -> MemorySelection:
    """Select relevant memories using model or keyword fallback.

    Parameters
    ----------
    query:
        The memory query containing alert summary and recent text.
    catalog:
        The list of available memory catalog entries.
    model:
        The language model to use for selection (must support ainvoke).
    limit:
        Maximum number of memories to select.

    Returns
    -------
    MemorySelection
        The selected memories with mode and reason.
    """
    if not catalog:
        return MemorySelection(filenames=[], mode="empty", reason="no memories available")

    # Build catalog context for the model
    catalog_text = "\n".join(
        f"- {entry.name}: {entry.description}"
        for entry in catalog
    )

    prompt = f"""Select up to {limit} most relevant project memories for the following incident.

## Incident Context
Alert: {query.alert_summary}
Recent Activity: {query.recent_text}

## Available Memories
{catalog_text}

Return a JSON object with:
- selected_memories: list of memory names (strings)
- reason: brief explanation of selection

Return only valid JSON, no other text."""

    try:
        response = await model.ainvoke(prompt)

        # Parse the response
        content = response.content if hasattr(response, "content") else str(response)

        # Try to extract JSON from the response
        # Look for JSON block in markdown code fence or raw JSON
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            # Try to find raw JSON
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                raise ValueError("No JSON found in model response")

        result = json.loads(json_str)

        if not isinstance(result, dict):
            raise ValueError("Response is not a JSON object")

        selected_names = result.get("selected_memories", [])
        reason = result.get("reason", "")

        if not isinstance(selected_names, list):
            raise ValueError("selected_memories is not a list")

        # Validate against catalog
        catalog_names = {entry.name for entry in catalog}
        validated_names = []
        seen = set()

        for name in selected_names:
            if not isinstance(name, str):
                continue
            if name in catalog_names and name not in seen:
                validated_names.append(name)
                seen.add(name)
                if len(validated_names) >= limit:
                    break

        if validated_names:
            return MemorySelection(
                filenames=validated_names,
                mode="model",
                reason=reason or "model selected relevant memories",
            )

    except Exception:
        # Fall through to keyword fallback
        pass

    # Keyword fallback
    query_text = f"{query.alert_summary} {query.recent_text}".strip()
    query_tokens = _tokenize(query_text)

    if not query_tokens:
        return MemorySelection(filenames=[], mode="empty", reason="no tokens to match")

    scored = _compute_keyword_scores(query_tokens, catalog)
    selected = [name for name, _ in scored[:limit]]

    if selected:
        return MemorySelection(
            filenames=selected,
            mode="keyword",
            reason=f"keyword match on {len(query_tokens)} tokens",
        )

    return MemorySelection(filenames=[], mode="empty", reason="no matching memories found")
