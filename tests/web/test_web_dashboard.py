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
