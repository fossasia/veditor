from fastapi import FastAPI

from app.queue import redis_conn
from app.routes import jobs, ops, reviews, talks

app = FastAPI(title="VEditor API")

app.include_router(ops.router)
app.include_router(talks.router)
app.include_router(reviews.router)
app.include_router(jobs.router)


@app.get("/health")
def health_check():
    redis_conn.ping()
    return {"status": "ok"}
