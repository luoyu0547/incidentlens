import os
import socket

import pytest

os.environ.setdefault("INCIDENTLENS_AGENT_MODE", "deterministic_baseline")


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if request.node.get_closest_marker("live_llm") or request.node.get_closest_marker("integration"):
        return

    def denied(*args, **kwargs):
        raise AssertionError("unit test attempted a real network connection")

    monkeypatch.setattr(socket, "create_connection", denied)
