import logging
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


def auth_context_processor(request: Request) -> dict[str, Any]:
    if hasattr(request, "state") and hasattr(request.state, "user"):
        return {"user": request.state.user}

    token = request.cookies.get("veditor_session")
    if not token:
        if hasattr(request, "state"):
            request.state.user = None
        return {"user": None}

    # 1. Check if token is an SSO session token
    sso_payload = decode_sso_token(token)
    if sso_payload:
        sso_user = CurrentUser(
            user_id=None,
            client_id=None,
            email=None,
            role=sso_payload["role"],
            source="sso",
            event_ids=[sso_payload["scope_id"]]
            if sso_payload.get("scope_type") == "event"
            else [],
            scope_type=sso_payload.get("scope_type"),
            scope_id=sso_payload.get("scope_id"),
        )
        if hasattr(request, "state"):
            request.state.user = sso_user
        return {"user": sso_user}

    # 2. Check if token is a standard user session token
    payload = decode_session_token(token)
    if not payload or not isinstance(payload, dict):
        if hasattr(request, "state"):
            request.state.user = None
        return {"user": None}

    user_id = payload.get("user_id")
    if not user_id:
        if hasattr(request, "state"):
            request.state.user = None
        return {"user": None}

    app_instance = getattr(request, "app", None)
    overrides = (
        getattr(app_instance, "dependency_overrides", {}) if app_instance else {}
    )
    db_factory = overrides.get(get_db, SessionLocal)

    res = db_factory()
    gen = None
    should_close = False
    if hasattr(res, "__next__"):
        gen = res
        db = next(gen)
    else:
        db = res
        if db_factory is SessionLocal:
            should_close = True

    user = None
    try:
        candidate = db.query(models.User).filter(models.User.id == user_id).first()
        if candidate is not None and candidate.is_active is True:
            db.expunge(candidate)
            user = candidate
    except SQLAlchemyError as exc:
        logger.debug("Failed to look up user from session token: %s", exc)
    finally:
        if gen is not None:
            gen.close()
        elif should_close:
            db.close()

    if hasattr(request, "state"):
        request.state.user = user
    return {"user": user}


templates = Jinja2Templates(env=_env, context_processors=[auth_context_processor])
