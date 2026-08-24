"""Shared fixtures for versioned product API tests.

The ``client`` fixture follows ``tests/web/conftest.py``: the real FastAPI
application built with a fake remote transport factory so no request touches
the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a test client whose session manager uses a fake transport."""
    app = create_app(
        RuntimeSettings(data_dir=tmp_path / "data"),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as client:
        yield client
