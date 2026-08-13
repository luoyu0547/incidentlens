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


def test_events_stream(client):
    """SSE stream is registered and starts with a 200 response.

    The sync TestClient buffers the full response body, so an infinite SSE
    stream would hang a plain ``client.get``. Instead, drive the ASGI app
    directly: the response start (200) is emitted before the first body
    chunk, and the immediate client disconnect cancels the generator so the
    app returns.
    """
    import asyncio

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

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def run() -> list[object]:
        started: list[object] = []

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                started.append(message["status"])

        await client.app(scope, receive, send)
        return started

    statuses = asyncio.run(run())
    assert statuses == [200]
