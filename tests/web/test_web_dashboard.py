"""Tests for the Jinja2+HTMX web dashboard routes.

The ``client`` fixture comes from ``tests/web/conftest.py``: it builds the
real FastAPI application via ``TestClient`` (which runs the lifespan, so
``app.state.runtime`` is populated) and injects a ``FakeTransportFactory``.
"""


def test_dashboard_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "IncidentLens" in resp.text


def test_dashboard_contains_investigation_list(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Dashboard should have an investigations section
    assert "investigation" in resp.text.lower() or "调查" in resp.text


def test_web_investigations_list(client):
    resp = client.get("/web/investigations")
    assert resp.status_code == 200


def test_investigation_detail_page(client):
    resp = client.get("/web/investigations/nonexistent")
    # Should return 200 with a "not found" message, not 500
    assert resp.status_code == 200
    assert "was not found" in resp.text.lower()
    # The queried id should be echoed in the not-found message
    assert "nonexistent" in resp.text


def test_investigation_detail_has_timeline_section(client, runtime):
    inv = runtime.investigations.create_investigation(
        project_id="proj-x",
        target_id="target-x",
        service="web",
        symptom="test",
    )
    resp = client.get(f"/web/investigations/{inv.investigation_id}")
    assert resp.status_code == 200
    assert "Timeline" in resp.text


def test_approvals_page(client, pending_approval):
    resp = client.get("/web/approvals")
    assert resp.status_code == 200
    assert "Approval" in resp.text
    # The intent kind (not an ``intent_type`` field) should be rendered.
    assert "docker.restart" in resp.text
    # The action buttons must carry the real approval id through the include.
    assert f"/web/approvals/{pending_approval}/approve" in resp.text
    assert f"/web/approvals/{pending_approval}/reject" in resp.text


def test_logs_search_page(client):
    resp = client.get("/web/logs/search")
    assert resp.status_code == 200


def test_projects_page(client):
    resp = client.get("/web/projects")
    assert resp.status_code == 200


def test_reports_page_not_found(client):
    resp = client.get("/web/reports/nonexistent")
    # Missing investigation renders the error template, not a 500.
    assert resp.status_code == 200
    assert "Report not available" in resp.text


def test_events_stream(client, runtime):
    """SSE stream emits correctly framed, serialized events.

    The sync TestClient buffers the full response body, so an infinite SSE
    stream would hang a plain ``client.get``. Instead, drive the ASGI app
    directly: publish a RuntimeEvent while the generator is subscribed, capture
    the ``http.response.body`` chunks, then let a client disconnect cancel the
    generator so the app returns.
    """
    import asyncio
    from datetime import UTC, datetime

    from incidentlens_control_plane.events.types import (
        RuntimeEvent,
        RuntimeEventType,
    )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/web/events/stream",
        "raw_path": b"/web/events/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def run() -> tuple[list[object], list[bytes]]:
        started: list[object] = []
        bodies: list[bytes] = []
        body_captured = asyncio.Event()
        request_complete = False

        async def receive() -> dict[str, object]:
            nonlocal request_complete
            if not request_complete:
                request_complete = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await body_captured.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                started.append(message["status"])
            elif message["type"] == "http.response.body":
                bodies.append(message.get("body", b"") or b"")
                if not body_captured.is_set():
                    body_captured.set()

        driver = asyncio.create_task(client.app(scope, receive, send))

        event = RuntimeEvent(
            event_id="evt-sse-1",
            sequence=0,
            event_type=RuntimeEventType.INVESTIGATION_CREATED,
            occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
            payload={"investigation_id": "inv-sse-1"},
        )
        # Publish until the route's generator has subscribed and forwarded a
        # framed chunk (the subscriber registers asynchronously).
        for _ in range(1000):
            await runtime.broker.publish(event)
            try:
                await asyncio.wait_for(body_captured.wait(), timeout=0.05)
                break
            except asyncio.TimeoutError:
                continue

        assert body_captured.is_set(), "no framed SSE body chunk was produced"
        await asyncio.wait_for(driver, timeout=5)
        return started, bodies

    started, bodies = asyncio.run(run())
    assert started == [200]
    assert bodies, "SSE stream produced no body chunks"
    framed = bodies[0]
    assert framed.startswith(b"data: ")
    assert b'"event_id":"evt-sse-1"' in framed
    assert b'"payload":{"investigation_id":"inv-sse-1"}' in framed
    assert framed.endswith(b"\n\n")
