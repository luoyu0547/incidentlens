"""Shared fixtures for Docker Compose integration tests.

Provides:
  - compose_urls: session-scoped fixture that brings up Docker Compose,
    waits for health, yields connection URLs, and tears down.
  - Docker availability check that fails explicitly if Docker is not present.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Generator

import httpx
import pytest

# Compose file path relative to project root
COMPOSE_FILE = "infra/compose/compose.yaml"

# How long to wait for each health endpoint
_HEALTH_TIMEOUT = 60  # seconds
_HEALTH_INTERVAL = 2  # seconds

# URLs used by the fixture
CONTROL_PLANE_URL = "http://localhost:8003"
GATEWAY_URL = "http://localhost:8000"


def _docker_available() -> bool:
    """Check whether the Docker runtime is available and running."""
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False
    try:
        result = subprocess.run(
            [docker_path, "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _wait_for_health(url: str, timeout: float = _HEALTH_TIMEOUT) -> bool:
    """Poll a /healthz endpoint until it responds 200 or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/healthz", timeout=5.0)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(_HEALTH_INTERVAL)
    return False


@pytest.fixture(scope="session")
def compose_urls() -> Generator[dict[str, str], None, None]:
    """Bring up Docker Compose, wait for health, yield URLs, tear down.

    Fails explicitly if Docker is not available rather than silently skipping.
    """
    if not _docker_available():
        pytest.fail(
            "Docker is not available or not running. "
            "Compose acceptance tests require a running Docker daemon."
        )

    # Start Compose services
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "--build", "-d"],
        check=True,
        timeout=300,
    )

    # Wait for both gateway and control-plane to be healthy
    gateway_ok = _wait_for_health(GATEWAY_URL)
    control_plane_ok = _wait_for_health(CONTROL_PLANE_URL)

    if not gateway_ok or not control_plane_ok:
        # Attempt cleanup before failing
        try:
            subprocess.run(
                ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v"],
                timeout=60,
            )
        except Exception:
            pass
        errors = []
        if not gateway_ok:
            errors.append("gateway-service")
        if not control_plane_ok:
            errors.append("control-plane")
        pytest.fail(
            f"Compose services did not become healthy: {', '.join(errors)}"
        )

    yield {
        "control_plane_url": CONTROL_PLANE_URL,
        "gateway_url": GATEWAY_URL,
    }

    # Tear down Compose services
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v"],
        check=True,
        timeout=120,
    )
