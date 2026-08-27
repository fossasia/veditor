from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.queue import redis_conn
from app.routes import ops, pipeline

app = FastAPI(title="VEditor API")

app.include_router(ops.router)
app.include_router(pipeline.router)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

DATA_DIR = Path("data")
if DATA_DIR.is_dir():
    app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fav = STATIC_DIR / "favicon.svg"
    return FileResponse(fav, media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/ui/")


@app.get("/health")
def health_check():
    redis_conn.ping()
    return {"status": "ok"}
