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
