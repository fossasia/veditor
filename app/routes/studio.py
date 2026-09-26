import logging
import urllib.parse
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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from redis.exceptions import RedisError
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, selectinload

from app import models
from app.auth import CurrentUser, hash_api_key
from app.config import settings
from app.db import get_db
from app.queue import light_queue
from app.routes.auth import _get_authenticated_user_from_cookie
from app.routes.talks import _cancel_talk_jobs
from app.security import decode_sso_token
from app.storage import StorageBackend, get_storage_backend
from app.tasks import job_waveform
from app.ui.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/studio", tags=["studio"])

# Characters that would end a URL path early. The ASGI path is decoded, so a
# room named "Q&A?" must have these re-escaped before the path is reused.
_PATH_DELIMITERS = str.maketrans({"%": "%25", "?": "%3F", "#": "%23"})


def _request_path_and_query(request: Request) -> tuple[str, str]:
    """Return the decoded path and raw query string from the ASGI scope.

    request.url re-parses the decoded path, so a "?" or "#" in a room name
    would cut the path short there; the scope keeps them intact.
    """
    path = request.scope.get("root_path", "") + request.scope["path"]
    query = request.scope.get("query_string", b"").decode("latin-1")
    return path, query


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
            detail="Invalid API Key. Please verify your credentials.",
        )
    return client


def get_optional_ui_client(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> models.Client | None:
    """Optional dependency that extracts API Key if present."""
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
    raw_sso = request.query_params.get("sso_token") or request.cookies.get(
        "veditor_session"
    )
    if raw_sso:
        sso_payload = decode_sso_token(raw_sso)
        if sso_payload:
            talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
            if not talk:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=not_found_detail,
                )
            authorized = (
                sso_payload.get("scope_type") == "talk"
                and sso_payload.get("scope_id") == talk.id
            ) or (
                sso_payload.get("scope_type") == "event"
                and sso_payload.get("scope_id") == talk.event_id
                and (
                    sso_payload.get("role") != "speaker"
                    or (
                        talk.speaker_email
                        and talk.speaker_email.lower() == sso_payload["email"].lower()
                    )
                )
            )
            if not authorized:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=not_found_detail,
                )
            if hasattr(request, "state"):
                request.state.user = CurrentUser(
                    email=sso_payload.get("email"),
                    display_name=sso_payload.get("display_name"),
                    role=sso_payload["role"],
                    source="sso",
                    event_ids=[sso_payload["scope_id"]]
                    if sso_payload.get("scope_type") == "event"
                    else [],
                    scope_type=sso_payload.get("scope_type"),
                    scope_id=sso_payload.get("scope_id"),
                )
            return talk

    user = _get_authenticated_user_from_cookie(request, db)
    if user:
        talk = (
            db.query(models.Talk)
            .options(selectinload(models.Talk.event))
            .filter(models.Talk.id == talk_id)
            .first()
        )
        if not talk:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=not_found_detail,
            )
        authorized = (
            user.role == "admin"
            or (
                user.role == "speaker"
                and talk.speaker_email
                and user.email
                and talk.speaker_email.lower() == user.email.lower()
            )
            or bool(talk.event and talk.event.created_by_user_id == user.id)
        )
        if not authorized:
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
                    authorized = True
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=not_found_detail,
            )
        if hasattr(request, "state"):
            request.state.user = user
        return talk

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
    talk = (
        db.query(models.Talk)
        .options(selectinload(models.Talk.event))
        .filter(models.Talk.id == talk_id)
        .first()
    )
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
        "title": "Timestamp Review",
        "desc": "Human verification of speaker In/Out points",
    },
    {
        "num": 3,
        "title": "Processing & Preview",
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
    "pending_approval": 0,
    "pending_intro_outro": 0,
    "rejected": 0,
    "pending_bounds": 1,
    "needs_work": 1,
    "cutting": 2,
    "generating_previews": 2,
    "preview": 2,
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
    event_id: str | None = None,
    status_filter: str | None = None,
    q: str | None = None,
    sso_token: str | None = None,
):
    # 1. Check if landing with ?sso_token=...
    if sso_token is not None or "sso_token" in request.query_params:
        raw_sso = (
            sso_token
            if sso_token is not None
            else request.query_params.get("sso_token")
        )
        if not raw_sso:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired SSO token",
            )
        sso_payload = decode_sso_token(raw_sso)
        if not sso_payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired SSO token",
            )
        is_secure = (request.url.scheme == "https") or (
            settings.environment.lower() in ("production", "prod")
        )
        if sso_payload["scope_type"] == "talk":
            dest_url = f"/studio/talks/{sso_payload['scope_id']}"
        else:
            dest_url = f"/studio?event_id={sso_payload['scope_id']}"
        resp = RedirectResponse(url=dest_url, status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(
            key="veditor_session",
            value=raw_sso,
            max_age=settings.sso_token_expire_seconds,
            httponly=True,
            samesite="lax",
            secure=is_secure,
            path="/",
        )
        return resp

    return _render_talks_page(
        request,
        db,
        client,
        event_id=event_id,
        status_filter=status_filter,
        q=q,
    )


@router.get("/rooms/{room_name:path}", response_class=HTMLResponse)
def room_talks(
    request: Request,
    room_name: str,
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[models.Client | None, Depends(get_optional_ui_client)] = None,
    event_id: str | None = None,
    status_filter: str | None = None,
    q: str | None = None,
):
    return _render_talks_page(
        request,
        db,
        client,
        event_id=event_id,
        status_filter=status_filter,
        q=q,
        room=room_name,
    )


def _resolve_event_id(
    event_id: str | int | None,
    scoped_events: list[models.Event],
) -> int | None:
    """Resolve an event_id given as a numeric id or as an external_id/slug.

    external_id is only unique per source, so a slug is resolved against the
    caller's own events. Anything else resolves to -1, which matches no talk.
    """
    if event_id is None:
        return None
    if isinstance(event_id, int):
        return event_id
    slug = str(event_id)
    if slug.isdigit():
        return int(slug)
    return next((e.id for e in scoped_events if e.external_id == slug), -1)


def _render_talks_page(
    request: Request,
    db: Session,
    client: models.Client | None,
    *,
    event_id: str | int | None,
    status_filter: str | None,
    q: str | None,
    room: str | None = None,
):
    """Render the talks list, scoped to the caller and optionally to an event/room."""
    # Check for authenticated user or active SSO session in cookie
    user = _get_authenticated_user_from_cookie(request, db)
    cookie_token = request.cookies.get("veditor_session")
    sso_user = decode_sso_token(cookie_token) if (not user and cookie_token) else None

    if request.headers.get("X-API-Key") and client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key. Please verify your credentials.",
        )

    if not user and not sso_user and client is None:
        # Send the user back to this exact page (path + filters) after login.
        # Only path delimiters are re-escaped so the whole target is encoded
        # once as the `next` value (spaces become %20, not %2520).
        path, query = _request_path_and_query(request)
        login_next = path.translate(_PATH_DELIMITERS)
        if query:
            login_next += f"?{query}"
        resp = RedirectResponse(
            url=f"/login?next={urllib.parse.quote(login_next, safe='/')}",
            status_code=status.HTTP_302_FOUND,
        )
        if request.cookies.get("veditor_api_key"):
            resp.delete_cookie("veditor_api_key")
        return resp

    if sso_user:
        if sso_user["scope_type"] == "talk":
            return RedirectResponse(
                url=f"/studio/talks/{sso_user['scope_id']}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        event_id = sso_user["scope_id"]
        user_events = db.query(models.Event).filter(models.Event.id == event_id).all()
    elif user:
        if user.role in ("organizer", "admin"):
            user_events = (
                db.query(models.Event)
                .filter(models.Event.created_by_user_id == user.id)
                .order_by(models.Event.name.asc())
                .all()
            )
        else:
            user_events = []
    else:
        user_events = (
            db.query(models.Event)
            .filter(models.Event.id.in_(client.event_ids or []))
            .order_by(models.Event.name.asc())
            .all()
        )
    if not sso_user:
        event_id = _resolve_event_id(event_id, user_events)

    # One scope drives the talk list, the room list and the stats: the talks the
    # caller may see, narrowed to the selected event. Speakers see their own
    # talks; everyone else sees the events they have access to (none for plain
    # users).
    if user and user.role == "speaker" and user.email:
        scope = func.lower(models.Talk.speaker_email) == user.email.lower()
    else:
        scope = models.Talk.event_id.in_([e.id for e in user_events])
        if sso_user and sso_user.get("role") == "speaker":
            scope = and_(
                scope,
                func.lower(models.Talk.speaker_email) == sso_user["email"].lower(),
            )
    if event_id is not None:
        scope = and_(scope, models.Talk.event_id == event_id)

    query = (
        db.query(models.Talk)
        .options(selectinload(models.Talk.jobs), selectinload(models.Talk.event))
        .filter(scope)
    )
    if room is not None:
        query = query.filter(models.Talk.room == room)
    if status_filter:
        query = query.filter(models.Talk.status == status_filter)

    talks = query.order_by(models.Talk.start.desc()).all()
    if q:
        q_lower = q.lower()
        talks = [t for t in talks if q_lower in t.title.lower()]

    all_rooms = [
        r
        for (r,) in db.query(models.Talk.room)
        .filter(scope, models.Talk.room.isnot(None), models.Talk.room != "")
        .distinct()
        .order_by(models.Talk.room)
    ]

    stats_scope = scope if room is None else and_(scope, models.Talk.room == room)
    status_counts: dict[str, int] = dict(
        db.query(models.Talk.status, func.count(models.Talk.id))
        .filter(stats_scope)
        .group_by(models.Talk.status)
        .all()
    )
    current_event = next((e for e in user_events if e.id == event_id), None)

    stats = {
        "total": sum(status_counts.values()),
        "pending": status_counts.get("pending_approval", 0),
        "processing": sum(
            status_counts.get(s, 0)
            for s in ("cutting", "generating_previews", "transcoding", "uploading")
        ),
        "preview": status_counts.get("preview", 0),
        "done": status_counts.get("done", 0),
        "broken": status_counts.get("broken", 0) + status_counts.get("rejected", 0),
    }

    flash_error = request.cookies.get("flash_error")

    response = templates.TemplateResponse(
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
            "error": flash_error,
            "current_event": current_event,
            "room": room,
            "filter_action": urllib.parse.quote(
                _request_path_and_query(request)[0], safe="/"
            ),
        },
        headers={"Cache-Control": "no-store"},
    )
    if request.cookies.get("flash_error"):
        response.delete_cookie("flash_error", path="/")
    return response


ALLOWED_MEDIA_CATEGORIES = frozenset(
    {"preview", "raw", "intro", "outro", "cut", "final"}
)


def _download_filename(talk: models.Talk, filename: str) -> str:
    safe_title = "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in (getattr(talk, "title", "") or "")
    ).strip("_")
    return f"{safe_title}_{filename}" if safe_title else f"talk_{talk.id}_{filename}"


@router.get("/media/{talk_id}/{filename}")
def get_talk_media_default(
    request: Request,
    talk_id: int,
    filename: str,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = _authorize_studio_talk(
        talk_id, request, db, not_found_detail="Media not found"
    )

    safe_filename = Path(filename).name
    candidate_keys = [
        f"{talk_id}/preview/{safe_filename}",
        f"{talk_id}/raw/{safe_filename}",
        f"{talk_id}/intro/{safe_filename}",
        f"{talk_id}/outro/{safe_filename}",
        f"{talk_id}/cut/{safe_filename}",
        f"{talk_id}/final/{safe_filename}",
    ]
    download_filename = (
        _download_filename(talk, safe_filename)
        if request.query_params.get("download") in ("1", "true", "yes")
        else None
    )

    for key in candidate_keys:
        if storage.exists(key):
            path = storage.get(key)
            return FileResponse(
                path,
                media_type="video/mp4",
                filename=download_filename,
                headers={"Cache-Control": "no-cache"},
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

    talk = _authorize_studio_talk(
        talk_id, request, db, not_found_detail="Media not found"
    )

    safe_filename = Path(filename).name
    key = f"{talk_id}/{safe_category}/{safe_filename}"
    if not storage.exists(key):
        raise HTTPException(status_code=404, detail=f"Media {key} not found")
    path = storage.get(key)

    download_filename = (
        _download_filename(talk, safe_filename)
        if request.query_params.get("download") in ("1", "true", "yes")
        else None
    )

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=download_filename,
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/talks/{talk_id}/waveform")
def get_talk_waveform(
    request: Request,
    talk_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    category: str | None = None,
    filename: str | None = None,
):
    _authorize_studio_talk(talk_id, request, db, not_found_detail="Talk not found")

    media_key = None
    if category and filename:
        safe_category = Path(category).name
        if safe_category not in ALLOWED_MEDIA_CATEGORIES:
            raise HTTPException(status_code=404, detail="Media not found")
        cand = f"{talk_id}/{safe_category}/{Path(filename).name}"
        if storage.exists(cand):
            media_key = cand
    else:
        candidates = [f"{talk_id}/{c}/{c}.mp4" for c in ("preview", "cut", "raw")]
        media_key = next((c for c in candidates if storage.exists(c)), None)
        if not media_key:
            raw_keys = storage.list_keys(f"{talk_id}/raw")
            media_key = raw_keys[0] if raw_keys else None

    if media_key:
        waveform_key = f"{media_key}.waveform.json"
        if storage.exists(waveform_key):
            try:
                cached_path = storage.get(waveform_key)
                # storage-boundary-exempt: read cached waveform json
                raw_bytes = cached_path.read_bytes()
                return Response(
                    content=raw_bytes,
                    media_type="application/json",
                    headers={"Cache-Control": "private, max-age=3600"},
                )
            except OSError:
                logger.warning("Failed reading cached waveform for %s", waveform_key)
        try:
            light_queue.enqueue(job_waveform, talk_id, media_key)
        except (OSError, RedisError, RuntimeError) as exc:
            logger.warning(
                "Failed to enqueue waveform generation for %s: %s", media_key, exc
            )

    return Response(
        content=b'{"peaks":[]}',
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/talks/{talk_id}", response_class=HTMLResponse)
def studio(
    request: Request,
    talk_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    client: Annotated[models.Client | None, Depends(get_optional_ui_client)] = None,
    sso_token: str | None = None,
):
    if sso_token is not None or "sso_token" in request.query_params:
        raw_sso = (
            sso_token
            if sso_token is not None
            else request.query_params.get("sso_token")
        )
        if not raw_sso:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired SSO token",
            )
        sso_payload = decode_sso_token(raw_sso)
        if not sso_payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired SSO token",
            )
        if sso_payload["scope_type"] == "talk" and sso_payload["scope_id"] != talk_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="SSO token is not authorized for this talk",
            )
        if sso_payload["scope_type"] == "event":
            talk_obj = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
            if not talk_obj:
                raise HTTPException(status_code=404, detail="Talk not found")
            if talk_obj.event_id != sso_payload["scope_id"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="SSO token is not authorized for this event",
                )
            if sso_payload.get("role") == "speaker" and not (
                talk_obj.speaker_email
                and talk_obj.speaker_email.lower() == sso_payload["email"].lower()
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="SSO token is not authorized for this talk",
                )

        is_secure = (request.url.scheme == "https") or (
            settings.environment.lower() in ("production", "prod")
        )
        resp = RedirectResponse(
            url=f"/studio/talks/{talk_id}", status_code=status.HTTP_303_SEE_OTHER
        )
        resp.set_cookie(
            key="veditor_session",
            value=raw_sso,
            max_age=settings.sso_token_expire_seconds,
            httponly=True,
            samesite="lax",
            secure=is_secure,
            path="/",
        )
        return resp

    user = _get_authenticated_user_from_cookie(request, db)
    cookie_token = request.cookies.get("veditor_session")
    sso_user = decode_sso_token(cookie_token) if cookie_token else None

    if "api_key" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Query parameter 'api_key' is not supported. Please provide X-API-Key header or veditor_api_key cookie.",
        )

    if request.headers.get("X-API-Key") and client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key. Please verify your credentials.",
        )

    if not user and not sso_user and client is None:
        resp = RedirectResponse(
            url=f"/login?next=/studio/talks/{talk_id}",
            status_code=status.HTTP_302_FOUND,
        )
        if request.cookies.get("veditor_api_key"):
            resp.delete_cookie("veditor_api_key")
        return resp

    talk = _authorize_studio_talk(
        talk_id, request, db, not_found_detail="Talk not found"
    )

    duration_seconds = None
    if talk.start and talk.end:
        duration_seconds = int((talk.end - talk.start).total_seconds())

    # Build categorized media assets that can be watched in the studio
    asset_defs = [
        ("final", "final.mp4", "Master Video (Final)"),
        ("preview", "preview.mp4", "Preview Video"),
        ("raw", "raw.mp4", "Raw Recording"),
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

    raw_keys = storage.list_keys(f"{talk.id}/raw")
    for rk in sorted(raw_keys)[:1]:
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

    final_asset = next((a for a in media_assets if a["category"] == "final"), None)
    if not final_asset and (final_keys := storage.list_keys(f"{talk.id}/final")):
        url = f"/studio/media/{talk.id}/final/{urllib.parse.quote(Path(final_keys[0]).name)}"
        final_asset = {
            "label": "Master Video (Final)",
            "category": "final",
            "url": url,
        }
        media_assets.append(final_asset)

    preview_urls = [a["url"] for a in media_assets]

    is_other_organizer_talk = bool(
        user
        and user.role == "admin"
        and (not talk.event or talk.event.created_by_user_id != user.id)
    )

    is_from_admin = bool(
        user
        and user.role == "admin"
        and (request.query_params.get("from") == "admin" or is_other_organizer_talk)
    )

    back_url = (
        (f"/admin/events/{talk.event_id}" if talk.event_id else "/admin/events")
        if is_from_admin
        else "/studio"
    )

    return templates.TemplateResponse(
        request,
        "studio.html.jinja",
        {
            "talk": talk,
            "milestones": get_evaluated_milestones(talk.status),
            "duration_seconds": duration_seconds,
            "media_assets": media_assets,
            "final_asset": final_asset,
            "preview_urls": preview_urls,
            "all_statuses": ALL_STATUSES,
            "is_speaker": (
                sso_user.get("role") if sso_user else getattr(user, "role", None)
            )
            == "speaker",
            "is_other_organizer_talk": is_other_organizer_talk,
            "is_from_admin": is_from_admin,
            "back_url": back_url,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/events", response_class=HTMLResponse)
def list_studio_events(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    cookie_token = request.cookies.get("veditor_session")
    if cookie_token and decode_sso_token(cookie_token) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not authorized to access event management",
        )

    user = _get_authenticated_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(
            url="/login?next=/studio/events",
            status_code=status.HTTP_302_FOUND,
        )

    if user.role not in ("organizer", "admin"):
        is_secure = (request.url.scheme == "https") or (
            settings.environment.lower() in ("production", "prod")
        )
        resp = RedirectResponse(
            url="/studio",
            status_code=status.HTTP_302_FOUND,
        )
        resp.set_cookie(
            key="flash_error",
            value="You do not have the permission to access that page",
            max_age=10,
            path="/",
            httponly=True,
            samesite="lax",
            secure=is_secure,
        )
        return resp

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
    cookie_token = request.cookies.get("veditor_session")
    if cookie_token and decode_sso_token(cookie_token) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not authorized to access event management",
        )

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
        url="/studio/events",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/events/{event_id}/edit", response_class=HTMLResponse)
def edit_studio_event(
    request: Request,
    event_id: int,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
):
    cookie_token = request.cookies.get("veditor_session")
    if cookie_token and decode_sso_token(cookie_token) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not authorized to access event management",
        )

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
    cookie_token = request.cookies.get("veditor_session")
    if cookie_token and decode_sso_token(cookie_token) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not authorized to access event management",
        )

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
