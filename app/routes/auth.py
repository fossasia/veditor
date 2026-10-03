import asyncio
import logging
import urllib.parse
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import settings
from app.db import get_db
from app.queue import light_queue, redis_conn
from app.security import (
    create_access_token,
    create_email_verification_token,
    create_session_token,
    decode_email_verification_token,
    decode_session_token,
    hash_password,
    is_valid_email,
    verify_password,
)
from app.tasks import job_send_verification_email
from app.ui.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_DUMMY_HASH = hash_password("veditor-timing-defense-sentinel")


def _get_safe_redirect_target(target: str | None, default: str = "/studio") -> str:
    """Validate and return a safe internal redirect path.

    Strictly verifies that scheme and netloc are empty, the path begins with a
    single '/', and prevents open-redirect attacks or infinite redirect loops
    back to authentication endpoints.
    """
    if not target or not isinstance(target, str):
        return default

    cleaned = target.strip()
    if not cleaned:
        return default

    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except ValueError:
        return default

    if parsed.scheme or parsed.netloc:
        return default

    if not parsed.path.startswith("/") or parsed.path.startswith(("//", "/\\")):
        return default

    unquoted_path = urllib.parse.unquote(parsed.path)
    if not unquoted_path.startswith("/") or unquoted_path.startswith(("//", "/\\")):
        return default

    normalized_path = parsed.path.rstrip("/").lower()
    if normalized_path in (
        "/login",
        "/logout",
        "/signup",
        "/verify-email",
        "/verify-email/pending",
        "/verify-email/resend",
    ):
        return default

    return cleaned


def _sync_speaker_role(user: models.User, db: Session, email: str) -> None:
    if (
        user.role == "user"
        and db.query(models.Talk).filter(models.Talk.speaker_email == email).first()
    ):
        user.role = "speaker"
        db.commit()


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
    next: str | None = None,
):
    if _get_authenticated_user_from_cookie(request, db) is not None:
        target = _get_safe_redirect_target(next, default="/studio")
        return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "login.html.jinja",
        {"error": None, "email": "", "next": next or ""},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    clean_email = email.strip().lower()
    if not clean_email or not password:
        return templates.TemplateResponse(
            request,
            "login.html.jinja",
            {
                "error": "Invalid email or password.",
                "email": clean_email,
                "next": next or "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = db.query(models.User).filter(models.User.email == clean_email).first()
    target_hash = user.hashed_password if user else _DUMMY_HASH
    valid_password = verify_password(password, target_hash)

    if not user or not user.is_active or not valid_password:
        return templates.TemplateResponse(
            request,
            "login.html.jinja",
            {
                "error": "Invalid email or password.",
                "email": clean_email,
                "next": next or "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not user.is_verified:
        return templates.TemplateResponse(
            request,
            "login.html.jinja",
            {
                "error": "Please verify your email address before signing in.",
                "email": clean_email,
                "next": next or "",
                "unverified": True,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    _sync_speaker_role(user, db, clean_email)

    token = create_session_token(user.id, user.role)
    redirect_target = _get_safe_redirect_target(next, default="/studio")
    response = RedirectResponse(
        url=redirect_target, status_code=status.HTTP_303_SEE_OTHER
    )
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

    has_talk = (
        db.query(models.Talk).filter(models.Talk.speaker_email == clean_email).first()
    )
    user_role = "speaker" if has_talk else "user"

    user = models.User(
        email=clean_email,
        hashed_password=hashed,
        role=user_role,
        is_active=True,
        is_verified=False,
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

    verification_token = create_email_verification_token(
        user_id=user.id,
        email=user.email,
        expires_in_hours=settings.email_verification_expire_hours,
    )
    enqueue_error = False
    try:
        light_queue.enqueue(
            job_send_verification_email,
            user.id,
            user.email,
            verification_token,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to enqueue verification email for %s: %s", user.email, exc)
        enqueue_error = True

    encoded_email = urllib.parse.quote(clean_email)
    redirect_url = f"/verify-email/pending?email={encoded_email}"
    if enqueue_error:
        redirect_url += "&error=delivery_failed"

    return RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


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

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email address not verified",
        )

    _sync_speaker_role(user, db, clean_email)

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


@router.get("/verify-email/pending", response_class=HTMLResponse)
def verify_email_pending_page(
    request: Request,
    email: str = "",
    error: str = "",
):
    error_msg = None
    if error == "delivery_failed":
        error_msg = (
            "We were unable to deliver the verification email automatically. "
            "Please click Resend Verification Email below to try again."
        )
    return templates.TemplateResponse(
        request,
        "verify_email_pending.html.jinja",
        {"email": email.strip(), "error": error_msg},
    )


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_confirm_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: str = "",
):
    """Render confirmation page for email verification without mutating account state.

    Protects against automated email security scanners pre-fetching GET links
    and prematurely consuming single-use verification tokens.
    """
    clean_token = token.strip()
    if not clean_token:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {"error": "Verification link is missing or invalid.", "email": ""},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    payload = decode_email_verification_token(clean_token)
    if not payload:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {
                "error": "Verification link is invalid or has expired.",
                "email": "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_id = payload.get("user_id")
    token_email = payload.get("email")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active or user.email != token_email:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {
                "error": "User account not found, deactivated, or email has changed.",
                "email": token_email or "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if user.is_verified:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {
                "error": "This email address has already been verified. Please sign in.",
                "email": user.email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return templates.TemplateResponse(
        request,
        "verify_email_confirm.html.jinja",
        {"email": user.email, "token": clean_token},
    )


@router.post("/verify-email")
def verify_email_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str, Form()] = "",
):
    """Perform atomic email verification and establish user session upon explicit user action."""
    clean_token = token.strip() or request.query_params.get("token", "").strip()
    if not clean_token:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {"error": "Verification link is missing or invalid.", "email": ""},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    payload = decode_email_verification_token(clean_token)
    if not payload:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {
                "error": "Verification link is invalid or has expired.",
                "email": "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_id = payload.get("user_id")
    token_email = payload.get("email")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active or user.email != token_email:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {
                "error": "User account not found, deactivated, or email has changed.",
                "email": token_email or "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    stmt = (
        update(models.User)
        .where(
            models.User.id == user.id,
            models.User.is_verified.is_(False),
            models.User.is_active.is_(True),
        )
        .values(is_verified=True, verified_at=datetime.now(UTC))
    )
    result = db.execute(stmt)
    db.commit()

    if result.rowcount == 0:
        return templates.TemplateResponse(
            request,
            "verify_email_error.html.jinja",
            {
                "error": "This email address has already been verified. Please sign in.",
                "email": user.email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    db.refresh(user)

    _sync_speaker_role(user, db, user.email)

    session_token = create_session_token(user.id, user.role)
    response = RedirectResponse(
        url="/studio?verified=1", status_code=status.HTTP_303_SEE_OTHER
    )
    is_secure = (request.url.scheme == "https") or settings.is_production
    response.set_cookie(
        key="veditor_session",
        value=session_token,
        max_age=settings.session_token_expire_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )
    return response


@router.get("/verify-email/resend", response_class=HTMLResponse)
def verify_email_resend_page(
    request: Request,
    email: str = "",
):
    return templates.TemplateResponse(
        request,
        "verify_email_resend.html.jinja",
        {"error": None, "message": None, "email": email.strip()},
    )


@router.post("/verify-email/resend", response_class=HTMLResponse)
def verify_email_resend_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()] = "",
):
    clean_email = email.strip().lower()
    if not clean_email or not is_valid_email(clean_email):
        return templates.TemplateResponse(
            request,
            "verify_email_resend.html.jinja",
            {
                "error": "Please enter a valid email address.",
                "message": None,
                "email": clean_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Rate limiting via Redis (60 seconds cooldown) - atomic NX
    rate_key = f"rate:resend_verification:{clean_email}"
    try:
        acquired = redis_conn.set(rate_key, "1", ex=60, nx=True)
        if not acquired:
            ttl = redis_conn.ttl(rate_key)
            wait_seconds = max(1, ttl) if ttl > 0 else 60
            return templates.TemplateResponse(
                request,
                "verify_email_resend.html.jinja",
                {
                    "error": f"Please wait {wait_seconds} seconds before requesting another verification email.",
                    "message": None,
                    "email": clean_email,
                },
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Redis rate limit check failed for %s: %s", clean_email, exc)
        return templates.TemplateResponse(
            request,
            "verify_email_resend.html.jinja",
            {
                "error": "The service is temporarily unavailable. Please try again later.",
                "message": None,
                "email": clean_email,
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    user = db.query(models.User).filter(models.User.email == clean_email).first()
    if user and user.is_active and not user.is_verified:
        token = create_email_verification_token(
            user_id=user.id,
            email=user.email,
            expires_in_hours=settings.email_verification_expire_hours,
        )
        try:
            light_queue.enqueue(
                job_send_verification_email,
                user.id,
                user.email,
                token,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to enqueue resend verification email for %s: %s",
                user.email,
                exc,
            )
            try:
                redis_conn.delete(rate_key)
            except Exception as del_exc:  # noqa: BLE001
                logger.debug(
                    "Failed to clear resend rate limit for %s: %s",
                    clean_email,
                    del_exc,
                )

    return templates.TemplateResponse(
        request,
        "verify_email_resend.html.jinja",
        {
            "error": None,
            "message": "If an unverified account exists with that email address, a new verification link has been sent.",
            "email": clean_email,
        },
    )
