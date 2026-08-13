"""Docker Compose 验收测试（需要 Docker 环境）。

默认跳过；设置 ``INCIDENTLENS_RUN_ACCEPTANCE=1`` 且 Docker Compose 环境
运行后才会执行。校验 Phase 5 故障注入环境的微服务健康与订单创建链路。
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INCIDENTLENS_RUN_ACCEPTANCE") != "1",
    reason="Acceptance tests require INCIDENTLENS_RUN_ACCEPTANCE=1",
)


@pytest.fixture(scope="module")
def compose_urls():
    """验证 Docker Compose 服务是否可达。"""
    return {
        "api_gateway": "http://localhost:8080",
        "order_service": "http://localhost:5001",
        "payment_service": "http://localhost:5002",
        "inventory_service": "http://localhost:5003",
    }


async def test_services_are_healthy(compose_urls):
    """所有服务应该健康。"""
    import httpx

    async with httpx.AsyncClient() as client:
        for name, url in compose_urls.items():
            resp = await client.get(f"{url}/health", timeout=5)
            assert resp.status_code == 200, f"{name} not healthy"


async def test_order_creation_normal(compose_urls):
    """正常情况下订单创建成功。"""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{compose_urls['order_service']}/orders",
            json={"user_id": "test-user", "total": 99.99},
            timeout=10,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "order_id" in data
