from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.queue import redis_conn
from app.routes import auth, client, jobs, ops, reviews, studio, talks

app = FastAPI(title="VEditor API")

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

app.include_router(auth.router)
app.include_router(client.router)
app.include_router(ops.router)
app.include_router(talks.router)
app.include_router(reviews.router)
app.include_router(jobs.router)
app.include_router(studio.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/studio")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health_check():
    redis_conn.ping()
    return {"status": "ok"}
