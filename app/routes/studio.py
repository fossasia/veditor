from pathlib import Path
from typing import Annotated

import jinja2
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from app import models
from app.auth import hash_api_key
from app.db import get_db
from app.storage import StorageBackend, get_storage_backend

_TEMPLATES_DIR = Path(__file__).parent.parent / "ui" / "templates"

# Disable cache to avoid Jinja2 3.1.5+ unhashable cache key issue
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_env)

router = APIRouter(prefix="/studio", tags=["studio"])


def get_ui_client(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> models.Client:
    """Dependency that extracts API Key from Header, Cookie, or Query Param."""
    api_key = (
        request.headers.get("X-API-Key")
        or request.cookies.get("veditor_api_key")
        or request.query_params.get("api_key")
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key. Please provide X-API-Key header, veditor_api_key cookie, or api_key query param.",
        )
    hashed_key = hash_api_key(api_key)
    client = (
        db.query(models.Client).filter(models.Client.hashed_key == hashed_key).first()
    )
    if not client:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return client


def get_optional_ui_client(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> models.Client | None:
    """Optional client dependency for public read pages."""
    api_key = (
        request.headers.get("X-API-Key")
        or request.cookies.get("veditor_api_key")
        or request.query_params.get("api_key")
    )
    if not api_key:
        return None
    hashed_key = hash_api_key(api_key)
    return (
        db.query(models.Client).filter(models.Client.hashed_key == hashed_key).first()
    )


ALL_STATUSES = [
    "waiting_for_files",
    "detecting",
    "pending_approval",
    "pending_bounds",
    "cutting",
    "generating_previews",
    "preview",
    "transcoding",
    "uploading",
    "needs_work",
    "done",
    "rejected",
    "broken",
]

MILESTONES_DEF = [
    {
        "num": 1,
        "title": "Ingest & Detect",
        "desc": "Recording ingestion & talk bounds detection",
    },
    {
        "num": 2,
        "title": "Timestamp Review (Gate 1)",
        "desc": "Human verification of speaker In/Out points",
    },
    {
        "num": 3,
        "title": "Processing & Preview (Gate 2)",
        "desc": "Cut, loudness, title slates & low-res preview",
    },
    {
        "num": 4,
        "title": "Transcode & Publish",
        "desc": "Final quality master encode & upload",
    },
]

STAGE_MILESTONE_MAP = {
    "waiting_for_files": 0,
    "detecting": 0,
    "pending_approval": 1,
    "pending_bounds": 1,
    "rejected": 1,
    "cutting": 2,
    "generating_previews": 2,
    "preview": 2,
    "needs_work": 2,
    "transcoding": 3,
    "uploading": 3,
    "done": 4,  # All milestones complete
    "broken": 3,
}


def get_evaluated_milestones(status: str) -> list[dict]:
    current_idx = STAGE_MILESTONE_MAP.get(status, 0)
    result = []
    for idx, m in enumerate(MILESTONES_DEF):
        item = dict(m)
        if status == "done" or current_idx > idx:
            item["state"] = "completed"
        elif current_idx == idx:
            if status in ("rejected", "broken"):
                item["state"] = "failed"
            else:
                item["state"] = "active"
        else:
            item["state"] = "pending"
        result.append(item)
    return result


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[models.Client | None, Depends(get_optional_ui_client)] = None,
    event_id: int | None = None,
    status_filter: str | None = None,
    q: str | None = None,
):
    query = db.query(models.Talk).options(selectinload(models.Talk.jobs))
    if client is not None:
        query = query.filter(models.Talk.event_id.in_(client.event_ids))
    if event_id is not None:
        if client is not None and event_id not in client.event_ids:
            query = query.filter(models.Talk.id == -1)
        else:
            query = query.filter(models.Talk.event_id == event_id)
    if status_filter:
        query = query.filter(models.Talk.status == status_filter)

    talks = query.order_by(models.Talk.start.desc()).all()
    if q:
        q_lower = q.lower()
        talks = [t for t in talks if q_lower in t.title.lower()]

    if client is not None:
        all_talks = (
            db.query(models.Talk)
            .filter(models.Talk.event_id.in_(client.event_ids))
            .all()
        )
    else:
        all_talks = db.query(models.Talk).all()

    all_rooms = sorted({t.room for t in all_talks if t.room})
    status_counts: dict[str, int] = {}
    for t in all_talks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    stats = {
        "total": len(all_talks),
        "pending": status_counts.get("pending_approval", 0),
        "processing": sum(
            status_counts.get(s, 0)
            for s in ("cutting", "generating_previews", "transcoding", "uploading")
        ),
        "preview": status_counts.get("preview", 0),
        "done": status_counts.get("done", 0),
        "broken": status_counts.get("broken", 0) + status_counts.get("rejected", 0),
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "talks": talks,
            "stats": stats,
            "all_statuses": ALL_STATUSES,
            "all_rooms": all_rooms,
            "q": q or "",
            "status_filter": status_filter or "",
            "event_id": event_id,
        },
    )


@router.get("/media/{talk_id}/{filename}")
def get_talk_media_default(
    talk_id: int,
    filename: str,
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    candidate_keys = [
        f"{talk_id}/preview/{filename}",
        f"{talk_id}/raw/{filename}",
        f"{talk_id}/intro/{filename}",
        f"{talk_id}/outro/{filename}",
        f"{talk_id}/cut/{filename}",
        f"{talk_id}/final/{filename}",
    ]
    for key in candidate_keys:
        if storage.exists(key):
            path = storage.get(key)
            return FileResponse(path, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Media not found")


@router.get("/media/{talk_id}/{category}/{filename}")
def get_talk_media_categorized(
    talk_id: int,
    category: str,
    filename: str,
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    key = f"{talk_id}/{category}/{filename}"
    if not storage.exists(key):
        raise HTTPException(status_code=404, detail=f"Media {key} not found")
    path = storage.get(key)
    return FileResponse(path, media_type="video/mp4")


@router.get("/talks/{talk_id}", response_class=HTMLResponse)
def studio(
    request: Request,
    talk_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    client: Annotated[models.Client | None, Depends(get_optional_ui_client)] = None,
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or (client is not None and talk.event_id not in client.event_ids):
        raise HTTPException(status_code=404, detail="Talk not found")

    jobs = (
        db.query(models.Job)
        .filter(models.Job.talk_id == talk_id)
        .order_by(models.Job.id.desc())
        .limit(10)
        .all()
    )

    duration_seconds = None
    if talk.start and talk.end:
        duration_seconds = int((talk.end - talk.start).total_seconds())

    # Build categorized media assets that can be watched in the studio
    asset_defs = [
        ("preview", "preview.mp4", "Preview Video"),
        ("raw", "raw.mp4", "Raw Recording"),
        ("intro", "intro.mp4", "Opening Title Slate"),
        ("outro", "outro.mp4", "Outro Slate"),
        ("cut", "cut.mp4", "Cut Talk Clip"),
        ("final", "final.mp4", "Master Video (Final)"),
    ]
    media_assets = []
    seen_urls = set()

    for cat, fname, label in asset_defs:
        key = f"{talk.id}/{cat}/{fname}"
        if storage.exists(key):
            url = f"/studio/media/{talk.id}/{cat}/{fname}"
            if url not in seen_urls:
                seen_urls.add(url)
                media_assets.append(
                    {
                        "label": label,
                        "category": cat,
                        "url": url,
                    }
                )

    import urllib.parse

    for rk in storage.list_keys(f"{talk.id}/raw"):
        fname = Path(rk).name
        url = f"/studio/media/{talk.id}/raw/{urllib.parse.quote(fname)}"
        if url not in seen_urls:
            seen_urls.add(url)
            media_assets.append(
                {
                    "label": "Raw Recording",
                    "category": "raw",
                    "url": url,
                }
            )

    preview_urls = [a["url"] for a in media_assets]

    return templates.TemplateResponse(
        request,
        "studio.html",
        {
            "talk": talk,
            "jobs": jobs,
            "milestones": get_evaluated_milestones(talk.status),
            "duration_seconds": duration_seconds,
            "media_assets": media_assets,
            "preview_urls": preview_urls,
            "all_statuses": ALL_STATUSES,
        },
    )
