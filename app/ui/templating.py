import logging
from datetime import UTC
from pathlib import Path
from typing import Any

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from app import models
from app.auth import CurrentUser
from app.db import SessionLocal, get_db
from app.security import decode_session_token, decode_sso_token

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html", "jinja", "xml"]),
    cache_size=0,
)
_env.globals["UTC"] = UTC


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    total = round(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    if s > 0:
        parts.append(f"{s}s")
    return " ".join(parts) if parts else "0s"


_env.filters["format_duration"] = format_duration


def format_timecode_filter(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "00:00:00.00"
    total_cs = round(seconds * 100)
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{cs:02d}"


_env.filters["timecode"] = format_timecode_filter


def auth_context_processor(request: Request) -> dict[str, Any]:
    user = getattr(getattr(request, "state", None), "user", None)
    if user is None:
        token = request.cookies.get("veditor_session")
        if token:
            sso_payload = decode_sso_token(token)
            if sso_payload:
                user = CurrentUser(
                    user_id=None,
                    client_id=None,
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
            else:
                payload = decode_session_token(token)
                user_id = payload.get("user_id") if isinstance(payload, dict) else None
                if user_id:
                    app_instance = getattr(request, "app", None)
                    overrides = (
                        getattr(app_instance, "dependency_overrides", {})
                        if app_instance
                        else {}
                    )
                    db_factory = overrides.get(get_db, SessionLocal)
                    res = db_factory()
                    gen = res if hasattr(res, "__next__") else None
                    db = next(gen) if gen is not None else res
                    try:
                        candidate = (
                            db.query(models.User)
                            .filter(models.User.id == user_id)
                            .first()
                        )
                        if candidate is not None and candidate.is_active is True:
                            db.expunge(candidate)
                            user = candidate
                    except SQLAlchemyError as exc:
                        logger.debug(
                            "Failed to look up user from session token: %s", exc
                        )
                    finally:
                        if gen is not None:
                            gen.close()
                        elif db_factory is SessionLocal:
                            db.close()
        if hasattr(request, "state"):
            request.state.user = user

    is_speaker = (
        not user
        or getattr(user, "role", None) not in ["organizer", "admin"]
        or getattr(user, "role", None) == "speaker"
    )
    return {"user": user, "is_speaker": is_speaker}


templates = Jinja2Templates(env=_env, context_processors=[auth_context_processor])
