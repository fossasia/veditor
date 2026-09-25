from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.main import _STATIC_DIR, _add_vary_accept, app, not_found_handler


def test_404_page_not_found_browser():
    client = TestClient(app)
    resp = client.get("/stud", headers={"Accept": "text/html,application/xhtml+xml"})
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Page not found" in resp.text
    assert "Go to Homepage" in resp.text
    assert "HTTP 404" in resp.text
    assert 'href="/"' in resp.text
    assert "/static/css/error.css" in resp.text
    assert "Accept" in resp.headers.get("Vary", "")


def test_404_page_not_found_xhtml():
    client = TestClient(app)
    resp = client.get("/stud", headers={"Accept": "application/xhtml+xml"})
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Page not found" in resp.text
    assert "Accept" in resp.headers.get("Vary", "")


def test_404_json_api():
    client = TestClient(app)
    resp = client.get("/stud", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json() == {"detail": "Not Found"}
    assert "Accept" in resp.headers.get("Vary", "")


def test_404_content_negotiation_quality_values():
    client = TestClient(app)
    # text/html;q=0 should reject HTML and fall back to JSON
    resp = client.get("/stud", headers={"Accept": "text/html;q=0, application/json"})
    assert resp.status_code == 404
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json() == {"detail": "Not Found"}
    assert "Accept" in resp.headers.get("Vary", "")

    # application/json has higher quality than text/html
    resp2 = client.get(
        "/stud", headers={"Accept": "application/json;q=0.9, text/html;q=0.5"}
    )
    assert resp2.status_code == 404
    assert "application/json" in resp2.headers.get("content-type", "")

    # text/html has higher quality than application/json
    resp3 = client.get(
        "/stud", headers={"Accept": "text/html;q=0.9, application/json;q=0.5"}
    )
    assert resp3.status_code == 404
    assert "text/html" in resp3.headers.get("content-type", "")

    # application/* with higher quality than text/html selects JSON
    resp4 = client.get(
        "/stud", headers={"Accept": "text/html;q=0.5, application/*;q=1"}
    )
    assert resp4.status_code == 404
    assert "application/json" in resp4.headers.get("content-type", "")


def test_404_root_path_support():
    client = TestClient(app, root_path="/custom-root")
    resp = client.get("/stud", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert 'href="/custom-root"' in resp.text
    assert "/custom-root/static/css/error.css" in resp.text

    # Following root redirect preserves root_path
    root_resp = client.get("/custom-root/", follow_redirects=False)
    assert root_resp.status_code == 307
    assert root_resp.headers["location"] == "/custom-root/studio"

    # Following the homepage link from the 404 page reaches the prefix-aware /custom-root/studio
    home_resp = client.get("/custom-root", follow_redirects=False)
    assert home_resp.status_code in (301, 307, 308)
    assert home_resp.headers["location"] == "http://testserver/custom-root/"

    studio_resp = client.get(home_resp.headers["location"], follow_redirects=False)
    assert studio_resp.status_code == 307
    assert studio_resp.headers["location"] == "/custom-root/studio"


def test_404_preserves_exception_headers_and_vary():
    # Instantiate an isolated FastAPI app to avoid mutating global app instance
    test_app = FastAPI()
    test_app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    test_app.add_exception_handler(404, not_found_handler)

    @test_app.get("/test-404-headers")
    def route_404_with_headers():
        raise HTTPException(
            status_code=404,
            detail="Item missing",
            headers={"Cache-Control": "no-store", "Vary": "Accept-Encoding"},
        )

    test_client = TestClient(test_app)

    # Browser request: should get HTML, Cache-Control: no-store, and Vary: Accept-Encoding, Accept
    resp_html = test_client.get("/test-404-headers", headers={"Accept": "text/html"})
    assert resp_html.status_code == 404
    assert "text/html" in resp_html.headers.get("content-type", "")
    assert resp_html.headers.get("cache-control") == "no-store"
    vary_html = [v.strip() for v in resp_html.headers.get("vary", "").split(",")]
    assert "Accept-Encoding" in vary_html
    assert "Accept" in vary_html

    # JSON request: should get JSON, Cache-Control: no-store, and Vary: Accept-Encoding, Accept
    resp_json = test_client.get(
        "/test-404-headers", headers={"Accept": "application/json"}
    )
    assert resp_json.status_code == 404
    assert "application/json" in resp_json.headers.get("content-type", "")
    assert resp_json.headers.get("cache-control") == "no-store"
    assert resp_json.json() == {"detail": "Item missing"}
    vary_json = [v.strip() for v in resp_json.headers.get("vary", "").split(",")]
    assert "Accept-Encoding" in vary_json
    assert "Accept" in vary_json


def test_404_html_filters_payload_headers():
    test_app = FastAPI()
    test_app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    test_app.add_exception_handler(404, not_found_handler)

    @test_app.get("/test-payload-headers")
    def route_payload_headers():
        raise HTTPException(
            status_code=404,
            detail="Item missing",
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "application/problem+json",
                "Content-Length": "999",
            },
        )

    test_client = TestClient(test_app)
    resp = test_client.get("/test-payload-headers", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert resp.headers.get("cache-control") == "no-cache"
    assert resp.headers.get("content-length") != "999"


def test_add_vary_accept():
    # Empty vary header
    h1 = {"Vary": ""}
    _add_vary_accept(h1)
    assert h1["Vary"] == "Accept"

    # Whitespace only vary header
    h2 = {"Vary": "   "}
    _add_vary_accept(h2)
    assert h2["Vary"] == "Accept"

    # No vary header
    h3 = {}
    _add_vary_accept(h3)
    assert h3["Vary"] == "Accept"

    # Existing tokens without Accept
    h4 = {"Vary": "Accept-Encoding"}
    _add_vary_accept(h4)
    assert h4["Vary"] == "Accept-Encoding, Accept"

    # Existing tokens with extra commas and whitespace
    h5 = {"Vary": " gzip, , deflate "}
    _add_vary_accept(h5)
    assert h5["Vary"] == "gzip, deflate, Accept"

    # Already has accept (case-insensitive)
    h6 = {"Vary": "accept"}
    _add_vary_accept(h6)
    assert h6["Vary"] == "accept"
