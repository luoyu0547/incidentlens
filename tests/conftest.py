import os
import socket

import pytest

os.environ.setdefault("INCIDENTLENS_AGENT_MODE", "deterministic_baseline")


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if (
        request.node.get_closest_marker("live_llm")
        or request.node.get_closest_marker("integration")
    ):
        return

    def denied(*args, **kwargs):
        raise AssertionError("unit test attempted a real network connection")

    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def telemetry_repo():
    from incidentlens_telemetry.database import create_engine
    from incidentlens_telemetry.repository import TelemetryRepository

    return TelemetryRepository(create_engine("sqlite:///:memory:"))


@pytest.fixture
def toolkit(telemetry_repo):
    from incidentlens_control_plane.tools.query import ReadOnlyToolkit

    return ReadOnlyToolkit(telemetry_repo)


@pytest.fixture
def investigation_audit_store(telemetry_repo):
    from incidentlens_control_plane.agent.state import InvestigationAuditStore

    return InvestigationAuditStore(telemetry_repo.engine)
