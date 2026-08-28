"""Authenticated principals for the product API.

Exposes the stable identity vocabulary (:class:`Principal`,
:class:`PrincipalScope`, :class:`AuthenticationMethod`), the
:class:`AuthService` that owns digest-verified bearer tokens and signed session
cookies, and the reusable dependency stack (:func:`get_principal`,
:func:`require_scopes`, :func:`authorize_target`).
"""

from incidentlens_control_plane.auth.dependencies import (
    authorize_target,
    get_principal,
    require_scopes,
)
from incidentlens_control_plane.auth.service import AuthService
from incidentlens_control_plane.auth.types import (
    AuthenticationMethod,
    Principal,
    PrincipalScope,
)

__all__ = [
    "AuthenticationMethod",
    "AuthService",
    "Principal",
    "PrincipalScope",
    "authorize_target",
    "get_principal",
    "require_scopes",
]
