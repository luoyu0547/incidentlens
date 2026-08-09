"""Lightweight configuration container for compaction runtime paths.

Used by the agent lifecycle to pass directory configuration
from the FastAPI lifespan to the agent graph middleware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CompactionRuntimeConfig:
    """Paths for compaction middleware stores.

    This is a plain data class (not a full runtime) because the
    CompactionMiddleware constructs its own stores from these paths
    at graph-build time.
    """

    session_dir: Path = field(default_factory=lambda: Path(".incidentlens/sessions"))
    transcript_dir: Path = field(default_factory=lambda: Path(".incidentlens/transcripts"))
    task_output_dir: Path = field(default_factory=lambda: Path(".incidentlens/task-outputs"))
