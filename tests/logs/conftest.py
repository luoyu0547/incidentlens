"""Shared fixtures for log pipeline tests."""

from __future__ import annotations

import pytest
from incidentlens_control_plane.project_registry.types import TargetRegistration


@pytest.fixture
def target_registration() -> TargetRegistration:
    return TargetRegistration(
        target_id="dev-a",
        host="dev-a.example.test",
        ssh_user="deploy",
        ssh_config_alias="dev-a",
    )
