import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import settings
from app.db import get_db
from app.security import (
    create_access_token,
    create_session_token,
    decode_session_token,
    hash_password,
    is_valid_email,
    verify_password,
)
from app.ui.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_DUMMY_HASH = hash_password("veditor-timing-defense-sentinel")


def _get_authenticated_user_from_cookie(
    request: Request, db: Session
) -> models.User | None:
    token = request.cookies.get("veditor_session")
    if not token:
        return None
    payload = decode_session_token(token)
    if not payload or not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id")
    if not user_id:
        return None
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and user.is_active:
        return user
    return None


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    if _get_authenticated_user_from_cookie(request, db) is not None:
        return RedirectResponse(url="/studio", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "login.html.jinja",
        {"error": None, "email": ""},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    clean_email = email.strip().lower()
    if not clean_email or not password:
        return templates.TemplateResponse(
            request,
            "login.html.jinja",
            {"error": "Invalid email or password.", "email": clean_email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = db.query(models.User).filter(models.User.email == clean_email).first()
    target_hash = user.hashed_password if user else _DUMMY_HASH
    valid_password = verify_password(password, target_hash)

    if not user or not user.is_active or not valid_password:
        return templates.TemplateResponse(
            request,
            "login.html.jinja",
            {"error": "Invalid email or password.", "email": clean_email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    token = create_session_token(user.id, user.role)
    response = RedirectResponse(url="/studio", status_code=status.HTTP_303_SEE_OTHER)
    is_secure = (request.url.scheme == "https") or (
        settings.environment.lower() in ("production", "prod")
    )
    response.set_cookie(
        key="veditor_session",
        value=token,
        max_age=settings.session_token_expire_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )
    return response


@router.get("/signup", response_class=HTMLResponse)
def signup_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    if _get_authenticated_user_from_cookie(request, db) is not None:
        return RedirectResponse(url="/studio", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "signup.html.jinja",
        {"error": None, "email": ""},
    )


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    clean_email = email.strip().lower()
    if len(clean_email) > 255 or not is_valid_email(clean_email):
        return templates.TemplateResponse(
            request,
            "signup.html.jinja",
            {"error": "Please enter a valid email address.", "email": clean_email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "signup.html.jinja",
            {
                "error": "Password must be at least 8 characters long.",
                "email": clean_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) > 256:
        return templates.TemplateResponse(
            request,
            "signup.html.jinja",
            {
                "error": "Password must not exceed 256 characters.",
                "email": clean_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "signup.html.jinja",
            {"error": "Passwords do not match.", "email": clean_email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    existing = db.query(models.User).filter(models.User.email == clean_email).first()
    if existing:
        return templates.TemplateResponse(
            request,
            "signup.html.jinja",
            {
                "error": "An account with this email already exists.",
                "email": clean_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    hashed = hash_password(password)

    user = models.User(
        email=clean_email,
        hashed_password=hashed,
        role="user",
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "signup.html.jinja",
            {
                "error": "An account with this email already exists.",
                "email": clean_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    db.refresh(user)

    token = create_session_token(user.id, user.role)
    response = RedirectResponse(url="/studio", status_code=status.HTTP_303_SEE_OTHER)
    is_secure = (request.url.scheme == "https") or (
        settings.environment.lower() in ("production", "prod")
    )
    response.set_cookie(
        key="veditor_session",
        value=token,
        max_age=settings.session_token_expire_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )
    return response


@router.post("/logout")
@router.get("/logout")
def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    is_secure = (request.url.scheme == "https") or (
        settings.environment.lower() in ("production", "prod")
    )
    response.delete_cookie(
        key="veditor_session",
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_secure,
    )
    return response


@router.post("/api/auth/token", response_model=schemas.TokenResponse)
async def api_auth_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    content_type = request.headers.get("content-type", "").lower()
    email_or_username = None
    password = None

    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                email_or_username = body.get("email") or body.get("username")
                password = body.get("password")
        except (ValueError, TypeError) as exc:
            logger.debug("Failed parsing JSON request body: %s", exc)
    else:
        try:
            form = await request.form()
            email_or_username = form.get("email") or form.get("username")
            password = form.get("password")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed parsing form request body: %s", exc)
        if not email_or_username and not password:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    email_or_username = body.get("email") or body.get("username")
                    password = body.get("password")
            except (ValueError, TypeError, RuntimeError) as exc:
                logger.debug("Failed fallback JSON parse: %s", exc)

    if not email_or_username or not password or not isinstance(password, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    clean_email = str(email_or_username).strip().lower()
    user = db.query(models.User).filter(models.User.email == clean_email).first()
    target_hash = user.hashed_password if user else _DUMMY_HASH

    valid_password = await asyncio.to_thread(
        verify_password,
        password,
        target_hash,
    )

    if not user or not user.is_active or not valid_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role,
        expires_in_seconds=settings.access_token_expire_seconds,
    )
    return schemas.TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.access_token_expire_seconds,
    )
