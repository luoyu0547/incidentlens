from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from incidentlens_control_plane.web_assets import mount_web_assets


def _app(root: Path) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    mount_web_assets(app, web_root=root)
    return app


def test_root_and_allowlisted_deep_links_serve_index(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>", encoding="utf-8")
    client = TestClient(_app(tmp_path))
    for path in "/", "/services/payments", "/issues/abc", "/investigations/run-1":
        response = client.get(path)
        assert response.status_code == 200
        assert response.text == "<html>app</html>"
        assert response.headers["cache-control"] == "no-cache"


def test_assets_are_immutable_and_unknown_paths_do_not_fallback(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app-abc.js").write_text("ok", encoding="utf-8")
    (tmp_path / "index.html").write_text("app", encoding="utf-8")
    client = TestClient(_app(tmp_path))
    asset = client.get("/assets/app-abc.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/missing").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/issues/missing.json").status_code == 404
    assert client.get("/unknown/path").status_code == 404


def test_missing_build_does_not_break_app_and_root_is_unavailable(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    assert client.get("/").status_code == 503
    assert client.get("/healthz").status_code == 200
