import logging
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, selectinload

from app import models
from app.auth import hash_api_key
from app.db import get_db
from app.routes.auth import _get_authenticated_user_from_cookie
from app.routes.talks import _cancel_talk_jobs
from app.storage import StorageBackend, get_storage_backend
from app.ui.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/studio", tags=["studio"])


def get_ui_client(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> models.Client:
    """Dependency that extracts API Key from Header or Cookie."""
    api_key = request.headers.get("X-API-Key") or request.cookies.get("veditor_api_key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key. Please provide X-API-Key header or veditor_api_key cookie.",
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
    api_key = request.headers.get("X-API-Key") or request.cookies.get("veditor_api_key")
    if not api_key:
        return None
    hashed_key = hash_api_key(api_key)
    return (
        db.query(models.Client).filter(models.Client.hashed_key == hashed_key).first()
    )


def _authorize_studio_talk(
    talk_id: int,
    request: Request,
    db: Session,
    not_found_detail: str = "Talk not found",
) -> models.Talk:
    """
    Authorizes access to a talk in studio endpoints.
    Supports:
    1. Authenticated human session users (veditor_session cookie):
       - admin: access to any talk
       - organizer/user: access to talks in events created by the user
    2. API Key machine clients (X-API-Key header or veditor_api_key cookie):
       - access if talk.event_id in client.event_ids
    Raises 401 if unauthenticated, 404 if talk does not exist or caller is unauthorized.
    """
    user = _get_authenticated_user_from_cookie(request, db)
    if user:
        talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
        if not talk:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=not_found_detail,
            )
        if user.role == "admin":
            return talk
        event = db.query(models.Event).filter(models.Event.id == talk.event_id).first()
        if event and event.created_by_user_id == user.id:
            return talk
        api_key = request.headers.get("X-API-Key") or request.cookies.get(
            "veditor_api_key"
        )
        if api_key:
            hashed_key = hash_api_key(api_key)
            client = (
                db.query(models.Client)
                .filter(models.Client.hashed_key == hashed_key)
                .first()
            )
            if client and talk.event_id in client.event_ids:
                return talk
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_found_detail,
        )

    api_key = request.headers.get("X-API-Key") or request.cookies.get("veditor_api_key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key. Please provide X-API-Key header or veditor_api_key cookie.",
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
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_found_detail,
        )
    return talk


ALL_STATUSES = [
    "waiting_for_files",
    "detecting",
    "pending_approval",
    "pending_bounds",
    "cutting",
    "generating_previews",
    "preview",
    "pending_intro_outro",
    "assembling",
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
    "pending_intro_outro": 3,
    "assembling": 3,
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
    user = _get_authenticated_user_from_cookie(request, db)
    if user:
        if user.role == "admin":
            user_events = db.query(models.Event).order_by(models.Event.name.asc()).all()
        else:
            user_events = (
                db.query(models.Event)
                .filter(models.Event.created_by_user_id == user.id)
                .order_by(models.Event.name.asc())
                .all()
            )
    elif client is not None:
        user_events = (
            db.query(models.Event)
            .filter(models.Event.id.in_(client.event_ids))
            .order_by(models.Event.name.asc())
            .all()
        )
    else:
        user_events = []

    query = db.query(models.Talk).options(selectinload(models.Talk.jobs))
    if user:
        if user.role == "organizer":
            org_event_ids = [e.id for e in user_events]
            query = query.filter(models.Talk.event_id.in_(org_event_ids))
            if event_id is not None:
                if event_id not in org_event_ids:
                    query = query.filter(models.Talk.id == -1)
                else:
                    query = query.filter(models.Talk.event_id == event_id)
        elif user.role == "admin":
            if event_id is not None:
                query = query.filter(models.Talk.event_id == event_id)
        else:
            query = query.filter(models.Talk.id == -1)
    elif client is not None:
        query = query.filter(models.Talk.event_id.in_(client.event_ids))
        if event_id is not None:
            if event_id not in client.event_ids:
                query = query.filter(models.Talk.id == -1)
            else:
                query = query.filter(models.Talk.event_id == event_id)
    else:
        if event_id is not None:
            query = query.filter(models.Talk.event_id == event_id)

    if status_filter:
        query = query.filter(models.Talk.status == status_filter)

    talks = query.order_by(models.Talk.start.desc()).all()
    if q:
        q_lower = q.lower()
        talks = [t for t in talks if q_lower in t.title.lower()]

    if user:
        if user.role == "organizer":
            org_event_ids = [e.id for e in user_events]
            all_talks = (
                db.query(models.Talk)
                .filter(models.Talk.event_id.in_(org_event_ids))
                .all()
            )
        elif user.role == "admin":
            all_talks = db.query(models.Talk).all()
        else:
            all_talks = []
    elif client is not None:
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
        "dashboard.html.jinja",
        {
            "talks": talks,
            "stats": stats,
            "all_statuses": ALL_STATUSES,
            "all_rooms": all_rooms,
            "q": q or "",
            "status_filter": status_filter or "",
            "event_id": event_id,
            "user_events": user_events,
        },
        headers={"Cache-Control": "no-store"},
    )


ALLOWED_MEDIA_CATEGORIES = frozenset(
    {"preview", "raw", "intro", "outro", "cut", "final"}
)


@router.get("/media/{talk_id}/{filename}")
def get_talk_media_default(
    request: Request,
    talk_id: int,
    filename: str,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    _authorize_studio_talk(talk_id, request, db, not_found_detail="Media not found")

    safe_filename = Path(filename).name
    candidate_keys = [
        f"{talk_id}/preview/{safe_filename}",
        f"{talk_id}/raw/{safe_filename}",
        f"{talk_id}/intro/{safe_filename}",
        f"{talk_id}/outro/{safe_filename}",
        f"{talk_id}/cut/{safe_filename}",
        f"{talk_id}/final/{safe_filename}",
    ]
    for key in candidate_keys:
        if storage.exists(key):
            path = storage.get(key)
            return FileResponse(
                path,
                media_type="video/mp4",
                headers={"Cache-Control": "no-store"},
            )
    raise HTTPException(status_code=404, detail="Media not found")


@router.get("/media/{talk_id}/{category}/{filename}")
def get_talk_media_categorized(
    request: Request,
    talk_id: int,
    category: str,
    filename: str,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    safe_category = Path(category).name
    if safe_category not in ALLOWED_MEDIA_CATEGORIES:
        raise HTTPException(status_code=404, detail="Media not found")

    _authorize_studio_talk(talk_id, request, db, not_found_detail="Media not found")

    safe_filename = Path(filename).name
    key = f"{talk_id}/{safe_category}/{safe_filename}"
    if not storage.exists(key):
        raise HTTPException(status_code=404, detail=f"Media {key} not found")
    path = storage.get(key)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/talks/{talk_id}", response_class=HTMLResponse)
def studio(
    request: Request,
    talk_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = _authorize_studio_talk(
        talk_id, request, db, not_found_detail="Talk not found"
    )

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
        "studio.html.jinja",
        {
            "talk": talk,
            "jobs": jobs,
            "milestones": get_evaluated_milestones(talk.status),
            "duration_seconds": duration_seconds,
            "media_assets": media_assets,
            "preview_urls": preview_urls,
            "all_statuses": ALL_STATUSES,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/events", response_class=HTMLResponse)
def list_studio_events(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = _get_authenticated_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role not in ("organizer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires minimum role 'organizer'",
        )

    if user.role == "admin":
        events = (
            db.query(models.Event)
            .options(selectinload(models.Event.created_by_user))
            .order_by(models.Event.id.asc())
            .all()
        )
    else:
        events = (
            db.query(models.Event)
            .options(selectinload(models.Event.created_by_user))
            .filter(models.Event.created_by_user_id == user.id)
            .order_by(models.Event.id.asc())
            .all()
        )

    return templates.TemplateResponse(
        request,
        "events.html.jinja",
        {
            "events": events,
            "error": None,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/events", response_class=HTMLResponse)
def create_studio_event(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
):
    user = _get_authenticated_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role not in ("organizer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires minimum role 'organizer'",
        )

    clean_name = name.strip()
    if not clean_name:
        if user.role == "admin":
            events = (
                db.query(models.Event)
                .options(selectinload(models.Event.created_by_user))
                .order_by(models.Event.id.asc())
                .all()
            )
        else:
            events = (
                db.query(models.Event)
                .options(selectinload(models.Event.created_by_user))
                .filter(models.Event.created_by_user_id == user.id)
                .order_by(models.Event.id.asc())
                .all()
            )
        return templates.TemplateResponse(
            request,
            "events.html.jinja",
            {
                "events": events,
                "error": "Event name is required.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
            headers={"Cache-Control": "no-store"},
        )

    event = models.Event(
        name=clean_name,
        created_by_user_id=user.id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    return RedirectResponse(
        url=f"/studio?event_id={event.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/events/{event_id}/edit", response_class=HTMLResponse)
def edit_studio_event(
    request: Request,
    event_id: int,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
):
    user = _get_authenticated_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role not in ("organizer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires minimum role 'organizer'",
        )

    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Event not found"
        )

    if user.role != "admin" and event.created_by_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not authorized to edit this event",
        )

    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Event name cannot be empty",
        )

    event.name = clean_name
    db.commit()
    return RedirectResponse(url="/studio/events", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/events/{event_id}/delete", response_class=HTMLResponse)
def delete_studio_event(
    request: Request,
    event_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    user = _get_authenticated_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role not in ("organizer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires minimum role 'organizer'",
        )

    event = (
        db.query(models.Event)
        .filter(models.Event.id == event_id)
        .with_for_update()
        .first()
    )
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Event not found"
        )

    if user.role != "admin" and event.created_by_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not authorized to delete this event",
        )

    talks = list(event.talks)
    for talk in talks:
        _cancel_talk_jobs(talk.id, storage)

    for talk in talks:
        db.query(models.Review).filter(models.Review.talk_id == talk.id).delete()
        db.query(models.Job).filter(models.Job.talk_id == talk.id).delete()
        db.delete(talk)

    db.delete(event)
    db.commit()
    return RedirectResponse(url="/studio/events", status_code=status.HTTP_303_SEE_OTHER)
