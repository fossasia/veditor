from collections.abc import MutableMapping
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.queue import redis_conn
from app.routes import admin, auth, client, events, jobs, ops, reviews, studio, talks
from app.ui.templating import templates

app = FastAPI(title="VEditor API")


def _get_effective_q(
    accept_items: list[tuple[str, float]], mime: str
) -> tuple[int, float]:
    target_type, _ = mime.split("/")
    best_spec = -1
    best_q = 0.0
    for pattern, q in accept_items:
        if pattern == mime:
            spec = 2
        elif pattern == f"{target_type}/*":
            spec = 1
        elif pattern == "*/*":
            spec = 0
        else:
            continue
        if spec > best_spec:
            best_spec = spec
            best_q = q
        elif spec == best_spec:
            best_q = max(best_q, q)
    return best_spec, best_q


def _prefers_html(accept: str | None) -> bool:
    if not accept:
        return False
    items: list[tuple[str, float]] = []
    for item in accept.split(","):
        parts = [p.strip() for p in item.split(";")]
        if not parts or not parts[0]:
            continue
        mime = parts[0].lower()
        q = 1.0
        for param in parts[1:]:
            if param.lower().startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
                break
        items.append((mime, max(0.0, min(1.0, q))))

    html_spec, html_q = _get_effective_q(items, "text/html")
    xhtml_spec, xhtml_q = _get_effective_q(items, "application/xhtml+xml")
    if (xhtml_q, xhtml_spec) > (html_q, html_spec):
        html_spec, html_q = xhtml_spec, xhtml_q

    json_spec, json_q = _get_effective_q(items, "application/json")

    if html_q <= 0.0:
        return False
    if html_q > json_q:
        return True
    if html_q == json_q:
        return html_spec > json_spec
    return False


def _add_vary_accept(headers: MutableMapping[str, str]) -> None:
    vary_key = next((k for k in headers if k.lower() == "vary"), None)
    if not vary_key:
        headers["Vary"] = "Accept"
        return
    tokens = [t.strip() for t in headers[vary_key].split(",") if t.strip()]
    if not any(t.lower() == "accept" for t in tokens):
        tokens.append("Accept")
        headers[vary_key] = ", ".join(tokens)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception) -> Response:
    exc_headers = dict(getattr(exc, "headers", None) or {})

    if _prefers_html(request.headers.get("accept")):
        response = templates.TemplateResponse(
            request, "404.html.jinja", {}, status_code=404
        )
        for k, v in exc_headers.items():
            if k.lower() not in ("content-type", "content-length"):
                response.headers[k] = v
        _add_vary_accept(response.headers)
        return response

    _add_vary_accept(exc_headers)
    return JSONResponse(
        {"detail": getattr(exc, "detail", "Not Found")},
        status_code=404,
        headers=exc_headers,
    )


_STATIC_DIR = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(client.router)
app.include_router(events.router)
app.include_router(ops.router)
app.include_router(talks.router)
app.include_router(reviews.router)
app.include_router(jobs.router)
app.include_router(studio.router)


@app.get("/", include_in_schema=False)
def root(request: Request):
    root_path = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root_path}/studio")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health_check():
    redis_conn.ping()
    return {"status": "ok"}
