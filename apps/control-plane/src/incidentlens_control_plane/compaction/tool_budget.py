"""Tool budget persistence layer.

Persists oversized tool results to disk with atomic writes and SHA-256 verification.
No model calls required - pure projection and persistence.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.compaction.domain import (
    CompactionLimits,
    CompactionOutcome,
    CompactionResult,
    ToolOutputReference,
)


class ToolOutputStore:
    """Persists oversized tool results to disk with atomic writes.

    Each tool output is written to a unique file named by its SHA-256 digest,
    ensuring deduplication and integrity verification.
    """

    def __init__(self, base_dir: Path | str) -> None:
        """Initialize the store with a base directory.

        Args:
            base_dir: Root directory for tool output storage. Will be created
                      if it does not exist.
        """
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _output_path(self, incident_id: str, digest_sha256: str) -> Path:
        """Get the path for a tool output file."""
        return self._base_dir / incident_id / f"{digest_sha256}.json"

    def _output_dir(self, incident_id: str) -> Path:
        """Get the directory for an incident's tool outputs."""
        return self._base_dir / incident_id

    def save(self, incident_id: str, content: str, metadata: dict[str, Any] | None = None) -> Path:
        """Atomically persist a tool output with metadata.

        Uses write-to-temp-then-rename pattern for crash safety.

        Args:
            incident_id: The incident identifier.
            content: The tool output content to persist.
            metadata: Optional metadata to include (e.g., tool_name, timestamp).

        Returns:
            Path to the persisted file.
        """
        # Calculate SHA-256 digest
        content_bytes = content.encode("utf-8")
        digest = hashlib.sha256(content_bytes).hexdigest()

        target_path = self._output_path(incident_id, digest)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Build file content
        file_content = {
            "content": content,
            "size_bytes": len(content_bytes),
            "digest_sha256": digest,
        }
        if metadata:
            file_content["metadata"] = metadata

        import json
        content_json = json.dumps(file_content, indent=2)

        # Write to temporary file in the same directory for atomic rename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=target_path.parent,
                suffix=".tmp",
                prefix=f"{digest[:8]}.",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content_json)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                # Clean up temp file on error
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            # Atomic rename
            try:
                os.replace(tmp_path, target_path)
            except BaseException:
                # Clean up temp file on replace failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise RuntimeError(f"Failed to persist tool output: {e}") from e

        return target_path

    def load(self, incident_id: str, digest_sha256: str) -> str | None:
        """Load a persisted tool output.

        Returns None if the output does not exist.
        """
        path = self._output_path(incident_id, digest_sha256)
        if not path.exists():
            return None

        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("content")
        except (json.JSONDecodeError, ValueError):
            # Corrupted file - return None rather than raising
            return None

    def exists(self, incident_id: str, digest_sha256: str) -> bool:
        """Check if a tool output exists."""
        return self._output_path(incident_id, digest_sha256).exists()


def _estimate_tokens(text: str) -> int:
    """Estimate token count from text.

    Uses a rough approximation of 4 characters per token.
    """
    return len(text) // 4


def _make_preview(content: str, max_size: int) -> str:
    """Create a preview of the content, truncated to max_size bytes."""
    preview_bytes = content.encode("utf-8")[:max_size]
    return preview_bytes.decode("utf-8", errors="ignore")


def persist_oversized_tool_results(
    messages: Sequence[Mapping[str, Any]],
    incident_id: str,
    store: ToolOutputStore,
    limits: CompactionLimits | None = None,
) -> CompactionResult:
    """Persist oversized tool results to disk until under budget.

    This function processes tool messages in order of size (largest first),
    persisting them to disk until the total size is under the limit.

    Args:
        messages: The message history containing tool results.
        incident_id: The incident identifier for storage paths.
        store: The ToolOutputStore for persistence.
        limits: Optional limits configuration. Uses defaults if None.

    Returns:
        CompactionResult with details of what was persisted.
    """
    if limits is None:
        limits = CompactionLimits()

    # Collect tool messages with their sizes
    tool_messages: list[tuple[int, Mapping[str, Any], int]] = []  # (index, msg, size)
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str):
                size = len(content.encode("utf-8"))
                tool_messages.append((i, msg, size))

    if not tool_messages:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={"reason": "no_tool_messages"},
        )

    # Sort by size (largest first)
    tool_messages.sort(key=lambda x: x[2], reverse=True)

    # Calculate total size of tool messages
    total_size = sum(size for _, _, size in tool_messages)
    messages_persisted = 0
    references: list[ToolOutputReference] = []

    # Persist largest results first until under budget
    for idx, msg, size in tool_messages:
        if total_size <= limits.max_tool_output_bytes:
            break

        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        # Persist to disk
        metadata = {
            "tool_call_id": msg.get("tool_call_id", ""),
            "original_index": idx,
        }
        path = store.save(incident_id, content, metadata)

        # Calculate digest
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Create preview
        preview = _make_preview(content, limits.preview_size_bytes)

        # Create reference
        ref = ToolOutputReference(
            path=str(path),
            size_bytes=size,
            digest_sha256=digest,
            preview=preview,
            reread_instruction=(
                f"Tool result for call {msg.get('tool_call_id', 'unknown')} "
                f"was persisted to {path}. Use the read_file tool to access it if needed."
            ),
        )
        references.append(ref)

        # Update total size
        total_size -= size
        messages_persisted += 1

    if messages_persisted == 0:
        return CompactionResult(
            outcome=CompactionOutcome.SKIPPED,
            messages_removed=0,
            messages_remaining=len(messages),
            details={"reason": "already_under_budget", "total_size": total_size},
        )

    return CompactionResult(
        outcome=CompactionOutcome.SUCCESS,
        messages_removed=messages_persisted,
        messages_remaining=len(messages) - messages_persisted,
        details={
            "persisted_count": messages_persisted,
            "total_size_before": total_size + sum(r.size_bytes for r in references),
            "total_size_after": total_size,
            "references": [r.model_dump() for r in references],
        },
    )
