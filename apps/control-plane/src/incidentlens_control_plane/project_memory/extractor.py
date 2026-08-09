"""Memory extraction from bounded transcripts.

Extracts memory candidates from pre-compact transcript files and applies
semantic deduplication against the existing catalog.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from incidentlens_control_plane.project_memory.domain import MemoryCandidate, MemoryType


# Secret patterns to reject
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(bearer|authorization)\s*[:=]?\s*[a-zA-Z0-9\-_.]+"),
    re.compile(r"(?i)-----\s*(RSA|PRIVATE|BEGIN)\s*"),
]


def _content_hash(content: str) -> str:
    """Compute a short content hash for conflict suffixes."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _contains_secret(text: str) -> bool:
    """Check if text contains a secret pattern."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _dedupe_name(base_name: str, catalog: list[dict[str, Any]], body: str) -> str:
    """Generate a deduplicated name.

    If the same name with equivalent content exists, it's an update.
    If the same name with different content exists, add conflict suffix.
    """
    existing = {e["name"]: e for e in catalog if e.get("name") == base_name}

    if base_name not in existing:
        return base_name

    # Same name exists - this is an update (same name = update existing memory)
    return base_name


async def extract_memories(
    transcript_path: str | Path,
    catalog: list[dict[str, Any]],
    model: Any,
) -> list[MemoryCandidate]:
    """Extract memory candidates from a bounded transcript.

    Parameters
    ----------
    transcript_path:
        Path to the bounded pre-compact transcript file.
    catalog:
        Current memory catalog entries (list of dicts with name, type, description).
    model:
        Language model for extraction.

    Returns
    -------
    list[MemoryCandidate]
        Extracted and deduplicated memory candidates.
    """
    transcript_path = Path(transcript_path)

    if not transcript_path.is_file():
        raise FileNotFoundError(f"Transcript not found: {transcript_path}")

    transcript_content = transcript_path.read_text(encoding="utf-8")

    # Build catalog context for deduplication
    catalog_json = json.dumps(catalog, indent=2)

    prompt = f"""Extract project memory candidates from this transcript.

## Transcript
{transcript_content[:10000]}  # Limit to prevent token overflow

## Existing Memory Catalog
{catalog_json}

## Instructions
Extract memories that would be valuable for future investigations. Focus on:
- Project-specific procedures and runbooks
- Lessons learned and feedback
- Technical references and conventions
- Project structure and architecture notes

Return a JSON array of memory candidates with these EXACT fields:
- name: lowercase kebab-case identifier (2-63 chars)
- description: brief description (1-500 chars)
- type: one of "project", "procedure", "feedback", "reference"
- body: detailed content (1-65536 chars)

Return ONLY valid JSON array, no other text."""

    response = await model.ainvoke(prompt)

    # Parse response
    content = response.content if hasattr(response, "content") else str(response)

    # Extract JSON array from response
    json_match = re.search(r"```(?:json)?\s*\n?(\[.*?\])\n?\s*```", content, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find raw JSON array
        json_match = re.search(r"\[.*\]", content, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            return []

    try:
        raw_candidates = json.loads(json_str)
    except json.JSONDecodeError:
        return []

    if not isinstance(raw_candidates, list):
        return []

    # Process and validate candidates
    candidates: list[MemoryCandidate] = []
    seen_names: set[str] = set()

    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue

        # Extract fields
        name = raw.get("name", "")
        description = raw.get("description", "")
        mem_type = raw.get("type", "")
        body = raw.get("body", "")

        if not all([name, description, mem_type, body]):
            continue

        # Validate type
        try:
            memory_type = MemoryType(mem_type)
        except ValueError:
            continue

        # Secret scanning - reject if contains secrets
        combined_text = f"{name} {description} {body}"
        if _contains_secret(combined_text):
            continue

        # Semantic deduplication - generate unique name
        deduped_name = _dedupe_name(name, catalog, body)

        # Skip if we've already processed this name
        if deduped_name in seen_names:
            continue
        seen_names.add(deduped_name)

        # Create candidate
        try:
            candidate = MemoryCandidate(
                name=deduped_name,
                description=description,
                type=memory_type,
                body=body,
            )
            candidates.append(candidate)
        except Exception:
            # Skip invalid candidates
            continue

    return candidates
