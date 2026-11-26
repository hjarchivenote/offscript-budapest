from fastapi.testclient import TestClient

from app import main


def test_deployment_revalidates_assets_and_does_not_cache_dynamic_views(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    asset = static / "app.js"
    asset.write_text("const release = 'first';")
    (static / "index.html").write_text("<h1>Application shell</h1>")
    monkeypatch.setattr(main, "STATIC", static)
    with TestClient(main.create_app("sqlite://")) as client:
        first = client.get("/static/app.js")
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-cache"
        cached = client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})
        assert cached.status_code == 304
        assert cached.headers["cache-control"] == "no-cache"
        asset.write_text("const release = 'second release with updated behavior';")
        updated = client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})
        assert updated.status_code == 200
        assert "second release" in updated.text
        assert updated.headers["etag"] != first.headers["etag"]
        for path in ("/", "/health", "/ready", "/version", "/openapi.json", "/api/config"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store", path
