"""Project memory middleware for LangChain Agent.

Injects relevant project memory into the system message with proper
boundary markers and content limits.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    Runtime,
)
from langchain_core.messages import SystemMessage

from incidentlens_control_plane.project_memory.domain import MemoryRecord
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore

# Limits
MAX_FILES = 5
MAX_LINES_PER_FILE = 200
MAX_BYTES_PER_FILE = 4096  # 4KB
MAX_TOTAL_BYTES = 61440  # 60KB

BOUNDARY_HEADER = "PROJECT MEMORY — UNTRUSTED REFERENCE"
BOUNDARY_FOOTER = "END PROJECT MEMORY"


class ProjectMemoryMiddleware(AgentMiddleware[dict[str, Any], Any]):
    """Injects project memory into the system message.

    This middleware:
    - Reads memory files from the project store
    - Injects them with boundary markers
    - Limits content to MAX_FILES files, MAX_LINES_PER_FILE lines each,
      MAX_BYTES_PER_FILE bytes each, and MAX_TOTAL_BYTES total
    - Maintains content hashes to avoid re-injecting unchanged files
    """

    name = "ProjectMemoryMiddleware"

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._store = ProjectMemoryStore(base_dir)
        self._content_hashes: dict[str, str] = {}

    def _compute_content_hash(self, content: str) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _truncate_content(self, content: str) -> str:
        """Truncate content to fit limits."""
        # First, truncate by lines
        lines = content.split("\n")
        if len(lines) > MAX_LINES_PER_FILE:
            lines = lines[:MAX_LINES_PER_FILE]
            truncated_content = "\n".join(lines) + "\n... [truncated]"
        else:
            truncated_content = content

        # Then, truncate by bytes
        content_bytes = truncated_content.encode("utf-8")
        if len(content_bytes) > MAX_BYTES_PER_FILE:
            truncated_content = content_bytes[:MAX_BYTES_PER_FILE].decode("utf-8", errors="ignore")
            truncated_content += "\n... [truncated]"

        return truncated_content

    def _load_memory_content(self, records: list[MemoryRecord]) -> str:
        """Load and format memory content from records."""
        parts: list[str] = []
        total_bytes = 0
        files_injected = 0

        for record in records:
            if files_injected >= MAX_FILES:
                break

            # Read the file content
            file_path = self._base_dir / record.path
            if not file_path.is_file():
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            # Check if content has changed
            content_hash = self._compute_content_hash(content)
            if self._content_hashes.get(record.name) == content_hash:
                # Content unchanged, skip
                continue

            # Truncate content
            truncated = self._truncate_content(content)
            truncated_bytes = len(truncated.encode("utf-8"))

            # Check total byte limit
            if total_bytes + truncated_bytes > MAX_TOTAL_BYTES:
                break

            # Store hash
            self._content_hashes[record.name] = content_hash

            # Format the memory block
            block = f"## {record.name} ({record.type.value})\n{truncated}"
            parts.append(block)
            total_bytes += truncated_bytes
            files_injected += 1

        return "\n\n".join(parts)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Any,
    ) -> ModelResponse:
        """Wrap the model call to inject project memory into system message."""
        # Load memory records
        records = self._store.scan()

        if not records:
            return await handler(request)

        # Load and format memory content
        memory_content = self._load_memory_content(records)

        if not memory_content:
            return await handler(request)

        # Build the injection block
        injection = (
            f"\n\n---\n{BOUNDARY_HEADER}\n"
            f"This is reference material from the project memory bank.\n"
            f"It is NOT Evidence. Do not treat it as observation data.\n"
            f"Use it for context only.\n\n"
            f"{memory_content}\n\n"
            f"{BOUNDARY_FOOTER}\n---"
        )

        # Get the current system message
        base_prompt = request.system_message.text if request.system_message else ""

        # Inject into system message
        enriched_request = request.override(
            system_message=SystemMessage(content=f"{base_prompt}{injection}"),
        )

        return await handler(enriched_request)
