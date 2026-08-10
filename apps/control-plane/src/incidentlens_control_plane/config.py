"""Local runtime configuration and service construction."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class RuntimeSettings(BaseModel):
    """Immutable settings for the local runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        """Create settings from the INCIDENTLENS_DATA_DIR environment variable."""
        configured = os.environ.get("INCIDENTLENS_DATA_DIR")
        data_dir = Path(configured).expanduser() if configured else Path.home() / ".incidentlens"
        return cls(data_dir=data_dir.resolve())
