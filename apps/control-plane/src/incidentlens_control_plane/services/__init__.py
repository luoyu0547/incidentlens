"""Control plane services package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from incidentlens_control_plane.services.demo_reset import DemoResetService
    from incidentlens_control_plane.services.investigation_export import (
        InvestigationExportService,
    )


def __getattr__(name: str):  # noqa: ANN001
    if name == "DemoResetService":
        from incidentlens_control_plane.services.demo_reset import DemoResetService
        return DemoResetService
    if name == "InvestigationExportService":
        from incidentlens_control_plane.services.investigation_export import (
            InvestigationExportService,
        )
        return InvestigationExportService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["DemoResetService", "InvestigationExportService"]
