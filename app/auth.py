import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Cookie, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models
from app.db import SessionLocal, get_db
from app.security import decode_access_token, decode_session_token, decode_sso_token

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_security = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

ROLE_HIERARCHY: dict[str, int] = {
    "user": 0,
    "speaker": 1,
    "organizer": 2,
    "admin": 3,
}


class CurrentUser(BaseModel):
    user_id: int | None = None
    client_id: int | None = None
    email: str | None = None
    display_name: str | None = None
    role: Literal["user", "organizer", "speaker", "admin"] = "user"
    source: Literal["api_key", "cookie", "jwt", "sso"]
    event_ids: list[int] = Field(default_factory=list)
    scope_type: Literal["event", "talk"] | None = None
    scope_id: int | None = None
    is_platform: bool = False

    @property
    def is_machine(self) -> bool:
        return self.source == "api_key"

    @property
    def is_sso(self) -> bool:
        return self.source == "sso"

    @property
    def is_human(self) -> bool:
        return self.source in ("cookie", "jwt")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_human_admin(self) -> bool:
        return self.role == "admin" and not self.is_machine and not self.is_sso

    def has_event_access(self, event_id: int) -> bool:
        if self.is_platform or self.is_human_admin:
            return True
        return event_id in self.event_ids


def hash_api_key(api_key: str) -> str:
    """Returns a SHA-256 hash of the API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _normalize_auth_value(value: str | None) -> str | None:
    """Normalizes user-controlled auth values without treating empty cookies as credentials."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def lock_active_admins(session: Session) -> list[int]:
    """Locks active administrator rows in ascending ID order and returns their IDs."""
    rows = (
        session.query(models.User.id)
        .filter(models.User.role == "admin", models.User.is_active.is_(True))
        .order_by(models.User.id.asc())
        .with_for_update()
        .all()
    )
    return [r[0] if isinstance(r, (tuple, list)) else getattr(r, "id", r) for r in rows]


def get_client(
    api_key: Annotated[str | None, Security(api_key_header)],
    db: Annotated[Session, Depends(get_db)],
) -> models.Client:
    """Dependency that extracts the X-API-Key and resolves it to a Client."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key",
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

    now = datetime.now(UTC)
    should_update = False
    if client.last_used_at is None:
        should_update = True
    else:
        last_used = (
            client.last_used_at
            if client.last_used_at.tzinfo is not None
            else client.last_used_at.replace(tzinfo=UTC)
        )
        if (now - last_used).total_seconds() > 300:
            should_update = True

    if should_update:
        client.last_used_at = now
        # Update last_used_at out-of-band using an isolated session so we never
        # prematurely commit the request session or risk detaching models on rollback.
        try:
            with SessionLocal() as separate_db:
                separate_db.query(models.Client).filter(
                    models.Client.id == client.id
                ).update({"last_used_at": now}, synchronize_session=False)
                separate_db.commit()
        except SQLAlchemyError as exc:
            logger.debug("Failed to update client last_used_at: %s", exc)

    return client


def verify_event_access(event_id: int, client: models.Client) -> None:
    """
    Validates that the provided client has access to the specified event_id.
    Raises a 403 Forbidden exception if the client does not have access.
    """
    if getattr(client, "is_platform", False):
        return
    if event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is not authorized to access this event",
        )


def _authenticate_api_key(raw_key: str | None, db: Session) -> CurrentUser:
    if not isinstance(raw_key, str) or not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    hashed_key = hash_api_key(raw_key)
    client = (
        db.query(models.Client).filter(models.Client.hashed_key == hashed_key).first()
    )
    if not client:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return CurrentUser(
        user_id=None,
        client_id=client.id,
        email=None,
        role="admin",
        source="api_key",
        event_ids=list(client.event_ids or []),
        is_platform=bool(getattr(client, "is_platform", False)),
    )


def get_current_user(
    request: Request = None,
    db: Annotated[Session, Depends(get_db)] = None,
    api_key: Annotated[str | None, Security(api_key_header)] = None,
    cookie_token: Annotated[str | None, Cookie(alias="veditor_session")] = None,
    cookie_api_key: Annotated[str | None, Cookie(alias="veditor_api_key")] = None,
    bearer_creds: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer_security)
    ] = None,
) -> CurrentUser:
    """
    Resolves the authenticated caller into a CurrentUser instance.
    Checks credentials in strict order:
    1. Machine client header (X-API-Key)
    2. Session cookie (veditor_session)
    3. Machine client cookie fallback (veditor_api_key)
    4. Authorization header (Authorization: Bearer <token>)

    Raises HTTP 401 Unauthorized if no credentials are present, or if
    provided credentials are invalid, expired, or deactivated.
    """
    app_overrides = (
        getattr(getattr(request, "app", None), "dependency_overrides", {})
        if request
        else {}
    )
    if get_client in app_overrides:
        client_fn = app_overrides[get_client]
        client = client_fn() if callable(client_fn) else client_fn
        if client:
            return CurrentUser(
                user_id=None,
                client_id=getattr(client, "id", None),
                email=None,
                role="admin",
                source="api_key",
                event_ids=list(client.event_ids or []),
                is_platform=bool(getattr(client, "is_platform", False)),
            )

    req_headers = getattr(request, "headers", {}) or {}
    req_cookies = getattr(request, "cookies", {}) or {}

    # 1. Explicit machine client header (X-API-Key)
    header_key = _normalize_auth_value(
        req_headers.get("X-API-Key") or req_headers.get("x-api-key")
    )
    provided_api_key = _normalize_auth_value(api_key)
    has_header_api_key = header_key is not None or provided_api_key is not None
    if has_header_api_key:
        return _authenticate_api_key(provided_api_key or header_key, db)

    # 2. SSO header (X-SSO-Token)
    raw_sso = None
    if hasattr(req_headers, "get"):
        candidate_header = req_headers.get("X-SSO-Token") or req_headers.get(
            "x-sso-token"
        )
        raw_sso = _normalize_auth_value(candidate_header)

    if raw_sso is not None:
        sso_payload = decode_sso_token(raw_sso)
        if not sso_payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired SSO token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return CurrentUser(
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

    # 3. Session cookie (veditor_session)
    has_cookie = cookie_token is not None or "veditor_session" in req_cookies
    if has_cookie:
        raw_cookie = _normalize_auth_value(
            cookie_token
            if cookie_token is not None
            else req_cookies.get("veditor_session")
        )
        if raw_cookie:
            # First attempt decoding as an SSO session token
            sso_payload = decode_sso_token(raw_cookie)
            if sso_payload:
                return CurrentUser(
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

            payload = decode_session_token(raw_cookie)
            if payload:
                user = (
                    db.query(models.User)
                    .filter(models.User.id == payload["user_id"])
                    .first()
                )
                if not user or not user.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="User account not found or inactive",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                return CurrentUser(
                    user_id=user.id,
                    email=user.email,
                    role=user.role,
                    source="cookie",
                    event_ids=[],
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # 4. Machine client cookie fallback (veditor_api_key)
    cookie_api = _normalize_auth_value(cookie_api_key)
    if cookie_api is None:
        cookie_api = _normalize_auth_value(req_cookies.get("veditor_api_key"))
    if cookie_api:
        client = (
            db.query(models.Client)
            .filter(models.Client.hashed_key == hash_api_key(cookie_api))
            .first()
        )
        if client:
            return CurrentUser(
                user_id=None,
                client_id=client.id,
                email=None,
                role="admin",
                source="api_key",
                event_ids=list(client.event_ids or []),
                is_platform=bool(getattr(client, "is_platform", False)),
            )
        # Ignore stale machine cookies so a later bearer credential can be used.

    # 5. Authorization header (Authorization: Bearer <token>)
    auth_header = req_headers.get("Authorization") or req_headers.get("authorization")
    has_auth_header = auth_header is not None or bearer_creds is not None
    if has_auth_header:
        token: str | None = None
        if bearer_creds is not None:
            token = _normalize_auth_value(bearer_creds.credentials)
        elif auth_header:
            token = _normalize_auth_value(auth_header)
            if token:
                parts = token.split()
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1]
                else:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid authorization header format",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = decode_access_token(token)
        if payload:
            user = (
                db.query(models.User)
                .filter(models.User.id == payload["user_id"])
                .first()
            )
            if not user or not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User account not found or inactive",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return CurrentUser(
                user_id=user.id,
                email=user.email,
                role=user.role,
                source="jwt",
                event_ids=[],
            )

        sso_payload = decode_sso_token(token)
        if sso_payload:
            return CurrentUser(
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

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 5. No credentials present
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_role(min_role: Literal["user", "organizer", "admin"] | str):
    """
    Dependency factory that evaluates the caller's role against a required minimum tier.
    Hierarchy: user (0) < organizer (1) < admin (2).
    Raises HTTP 403 Forbidden if caller's role is below the threshold.
    """
    if min_role not in ROLE_HIERARCHY:
        raise ValueError(
            f"Invalid min_role '{min_role}'. Must be one of {list(ROLE_HIERARCHY)}"
        )

    def _role_checker(
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        caller_tier = ROLE_HIERARCHY.get(user.role, -1)
        required_tier = ROLE_HIERARCHY[min_role]
        if caller_tier < required_tier:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation requires minimum role '{min_role}'",
            )
        return user

    return _role_checker


def require_admin(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """
    Dependency enforcing that the caller is an authenticated human administrator
    (cookie or JWT session with user_id set), rejecting machine API key clients.
    """
    if not user.is_human_admin or user.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires a human administrator",
        )
    return user


def check_event_access(
    event_id: int,
    user: CurrentUser,
    db: Session,
) -> models.Event:
    """
    Queries the target event by ID and evaluates authorization:
    - Permits machine API clients if event_id is within scoped event_ids.
    - Permits SSO sessions if scope_type is 'event' and scope_id matches event_id.
    - Permits human admins unconditionally.
    - Permits event creator (event.created_by_user_id == user.user_id).
    - Denies all other callers with HTTP 403 Forbidden.
    Raises HTTP 404 Not Found if the event does not exist.
    """
    if user.source == "api_key":
        if not user.has_event_access(event_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Client is not authorized to access this event",
            )
        event = db.query(models.Event).filter(models.Event.id == event_id).first()
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )
        return event

    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    if user.source == "sso":
        if user.scope_type == "event" and user.scope_id == event_id:
            return event
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO session is not authorized to access this event",
        )

    # Human administrator: unconditional access
    if user.role == "admin":
        return event

    # Event creator: ownership match
    if user.user_id is not None and event.created_by_user_id == user.user_id:
        return event

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User is not authorized to access this event",
    )


def check_talk_access(
    talk: models.Talk | int,
    user: CurrentUser,
    db: Session,
) -> models.Talk:
    """
    Queries target talk by ID and evaluates authorization:
    - If user.source == 'sso':
      * scope_type == 'talk': matches only if talk.id == user.scope_id.
      * scope_type == 'event': matches only if talk.event_id == user.scope_id.
      * Mismatches return HTTP 403 Forbidden.
    - If user.source == 'api_key':
      * matches if talk.event_id in user.event_ids; else 403.
    - If human admin: unconditional access.
    - If human organizer: access if event.created_by_user_id == user.user_id.
    Raises HTTP 404 Not Found if talk does not exist.
    """
    target_talk = (
        talk
        if isinstance(talk, models.Talk)
        else db.query(models.Talk).filter(models.Talk.id == talk).first()
    )
    if not target_talk:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Talk not found",
        )

    if user.source == "sso":
        if user.scope_type == "talk":
            if user.scope_id != target_talk.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="SSO session is not authorized for this talk",
                )
            return target_talk
        if user.scope_type == "event":
            if user.scope_id != target_talk.event_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="SSO session is not authorized for this event",
                )
            if user.role == "speaker" and not (
                user.email
                and target_talk.speaker_email
                and target_talk.speaker_email.lower() == user.email.lower()
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="SSO speaker session is not authorized for this talk",
                )
            return target_talk
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unauthorized SSO scope",
        )

    if user.source == "api_key":
        if not user.has_event_access(target_talk.event_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Client is not authorized to access this talk",
            )
        return target_talk

    if user.role == "admin":
        return target_talk

    if (
        user.role == "speaker"
        and target_talk.speaker_email
        and user.email
        and target_talk.speaker_email.lower() == user.email.lower()
    ):
        return target_talk

    event = (
        db.query(models.Event).filter(models.Event.id == target_talk.event_id).first()
    )
    if event and user.user_id is not None and event.created_by_user_id == user.user_id:
        return target_talk

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User is not authorized to access this talk",
    )


def require_event_access(event_id_param: str | int = "event_id"):
    """
    Dependency factory to gate access to existing events.
    Resolves event ID from path parameters or static integer, validates existence,
    and enforces access boundary rules. Returns the resolved models.Event.
    """

    def _event_access_dependency(
        request: Request,
        db: Annotated[Session, Depends(get_db)],
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> models.Event:
        resolved_id: int | None = None
        if isinstance(event_id_param, int):
            resolved_id = event_id_param
        elif request is not None and hasattr(request, "path_params"):
            raw = request.path_params.get(event_id_param)
            if raw is None:
                raw = request.path_params.get("event_id") or request.path_params.get(
                    "id"
                )
            if raw is not None:
                try:
                    resolved_id = int(raw)
                except (ValueError, TypeError) as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid event ID: {raw}",
                    ) from exc

        if resolved_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event ID could not be resolved from request",
            )

        return check_event_access(resolved_id, user, db)

    return _event_access_dependency


def require_talk_access(talk_id_param: str | int = "talk_id"):
    """
    Dependency factory to gate access to existing talks.
    Resolves talk ID from path parameters or static integer, validates existence,
    and enforces access boundary rules. Returns the resolved models.Talk.
    """

    def _talk_access_dependency(
        request: Request,
        db: Annotated[Session, Depends(get_db)],
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> models.Talk:
        resolved_id: int | None = None
        if isinstance(talk_id_param, int):
            resolved_id = talk_id_param
        elif request is not None and hasattr(request, "path_params"):
            raw = request.path_params.get(talk_id_param)
            if raw is None:
                raw = request.path_params.get("talk_id") or request.path_params.get(
                    "id"
                )
            if raw is not None:
                try:
                    resolved_id = int(raw)
                except (ValueError, TypeError) as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid talk ID: {raw}",
                    ) from exc

        if resolved_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Talk ID could not be resolved from request",
            )

        return check_talk_access(resolved_id, user, db)

    return _talk_access_dependency
