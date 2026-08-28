"""Docker Compose acceptance tests for deterministic routing and regressions."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INCIDENTLENS_RUN_ACCEPTANCE") != "1",
    reason="Acceptance tests require INCIDENTLENS_RUN_ACCEPTANCE=1",
)


@pytest.fixture(scope="module")
def compose_urls():
    return {
        "api_gateway": "http://127.0.0.1:8080",
        "order_stable": "http://127.0.0.1:5001",
        "order_canary": "http://127.0.0.1:5002",
        "payment_service": "http://127.0.0.1:5003",
        "inventory_service": "http://127.0.0.1:5004",
    }


async def test_services_are_healthy(compose_urls):
    import httpx

    async with httpx.AsyncClient() as client:
        for name, url in compose_urls.items():
            response = await client.get(f"{url}/health", timeout=5)
            assert response.status_code == 200, f"{name} not healthy"


async def test_routing_is_deterministic(compose_urls):
    import httpx

    async with httpx.AsyncClient() as client:
        for route, key in (("stable", "route-a"), ("canary", "route-b")):
            response = await client.post(
                f"{compose_urls['api_gateway']}/orders",
                json={"user_id": "routing", "total": 10},
                headers={"X-Route-Key": key, "X-Request-ID": f"route-{route}"},
                timeout=10,
            )
            assert response.headers["X-Served-By"] == route


async def test_dual_regression_pre_repair_matrix(compose_urls):
    import httpx

    expected = {
        ("route-a", 10): (201, "stable"),
        ("route-a", 500): (429, "stable"),
        ("route-b", 10): (503, "canary"),
        ("route-b", 500): (503, "canary"),
    }
    async with httpx.AsyncClient() as client:
        for (route_key, amount), (status, served_by) in expected.items():
            response = await client.post(
                f"{compose_urls['api_gateway']}/orders",
                json={"user_id": "matrix", "total": amount},
                headers={
                    "X-Route-Key": route_key,
                    "X-Request-ID": f"matrix-{route_key}-{amount}",
                },
                timeout=10,
            )
            assert response.status_code == status
            assert response.headers["X-Served-By"] == served_by
