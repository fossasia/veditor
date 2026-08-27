from fastapi.testclient import TestClient

from app.main import app


def test_root_redirects_to_ui():
    """Verify GET / redirects to /ui/."""
    client = TestClient(app, follow_redirects=False)
    response = client.get("/")
    assert response.status_code in (307, 308, 302, 301)
    assert response.headers["location"] == "/ui/"


def test_ui_index_served():
    """Verify GET /ui/ serves index.html with 200 OK."""
    client = TestClient(app)
    response = client.get("/ui/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "VEditor" in response.text
    assert "talksTableBody" in response.text


def test_ui_static_assets():
    """Verify CSS and JS static assets are served properly."""
    client = TestClient(app)

    css_resp = client.get("/ui/styles.css")
    assert css_resp.status_code == 200
    assert "text/css" in css_resp.headers["content-type"]
    assert "--brand-primary" in css_resp.text

    js_resp = client.get("/ui/app.js")
    assert js_resp.status_code == 200
    assert (
        "javascript" in js_resp.headers["content-type"]
        or "application/javascript" in js_resp.headers["content-type"]
    )
    assert "fetchTalks" in js_resp.text
