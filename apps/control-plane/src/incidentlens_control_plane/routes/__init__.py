"""Route package marker."""

from typing import cast

from fastapi import Request

from incidentlens_control_plane.runtime import RuntimeServices


def get_runtime(request: Request) -> RuntimeServices:
    return cast(RuntimeServices, request.app.state.runtime)

