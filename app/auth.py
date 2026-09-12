import hashlib
from typing import Annotated, Literal

from fastapi import Cookie, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.db import get_db
from app.security import decode_access_token, decode_session_token

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_security = HTTPBearer(auto_error=False)

ROLE_HIERARCHY: dict[str, int] = {
    "user": 0,
    "organizer": 1,
    "admin": 2,
}


class CurrentUser(BaseModel):
    user_id: int | None = None
    email: str | None = None
    role: Literal["user", "organizer", "admin"] = "user"
    source: Literal["api_key", "cookie", "jwt"]
    event_ids: list[int] = Field(default_factory=list)

    @property
    def is_machine(self) -> bool:
        return self.source == "api_key"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_human_admin(self) -> bool:
        return self.role == "admin" and not self.is_machine


def hash_api_key(api_key: str) -> str:
    """Returns a SHA-256 hash of the API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


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

    return client


def verify_event_access(event_id: int, client: models.Client) -> None:
    """
    Validates that the provided client has access to the specified event_id.
    Raises a 403 Forbidden exception if the client does not have access.
    """
    if event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is not authorized to access this event",
        )


def get_current_user(
    request: Request = None,
    db: Annotated[Session, Depends(get_db)] = None,
    api_key: Annotated[str | None, Security(api_key_header)] = None,
    cookie_token: Annotated[str | None, Cookie(alias="veditor_session")] = None,
    bearer_creds: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer_security)
    ] = None,
) -> CurrentUser:
    """
    Resolves the authenticated caller into a CurrentUser instance.
    Checks credentials in strict order:
    1. Machine client header (X-API-Key)
    2. Session cookie (veditor_session)
    3. Authorization header (Authorization: Bearer <token>)

    Raises HTTP 401 Unauthorized if no credentials are present, or if
    provided credentials are invalid, expired, or deactivated.
    """
    req_headers = request.headers if request is not None else {}
    req_cookies = request.cookies if request is not None else {}

    # 1. Machine client header (X-API-Key)
    has_api_key = (
        api_key is not None or "X-API-Key" in req_headers or "x-api-key" in req_headers
    )
    if has_api_key:
        raw_key = (
            api_key
            if api_key is not None
            else (req_headers.get("X-API-Key") or req_headers.get("x-api-key"))
        )
        if not raw_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API Key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        hashed_key = hash_api_key(raw_key)
        client = (
            db.query(models.Client)
            .filter(models.Client.hashed_key == hashed_key)
            .first()
        )
        if not client:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API Key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return CurrentUser(
            user_id=None,
            email=None,
            role="admin",
            source="api_key",
            event_ids=list(client.event_ids or []),
        )

    # 2. Session cookie (veditor_session)
    has_cookie = cookie_token is not None or "veditor_session" in req_cookies
    if has_cookie:
        raw_cookie = (
            cookie_token
            if cookie_token is not None
            else req_cookies.get("veditor_session")
        )
        if not raw_cookie:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = decode_session_token(raw_cookie)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = (
            db.query(models.User).filter(models.User.id == payload["user_id"]).first()
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

    # 3. Authorization header (Authorization: Bearer <token>)
    auth_header = req_headers.get("Authorization") or req_headers.get("authorization")
    has_auth_header = auth_header is not None or bearer_creds is not None
    if has_auth_header:
        token: str | None = None
        if bearer_creds is not None:
            token = bearer_creds.credentials
        elif auth_header:
            parts = auth_header.split()
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
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = (
            db.query(models.User).filter(models.User.id == payload["user_id"]).first()
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

    # 4. No credentials present
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
    - Permits human admins unconditionally.
    - Permits event creator (event.created_by_user_id == user.user_id).
    - Denies all other callers with HTTP 403 Forbidden.
    Raises HTTP 404 Not Found if the event does not exist.
    """
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    # Machine API client: strictly bound to scoped event_ids
    if user.source == "api_key":
        if event_id not in user.event_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Client is not authorized to access this event",
            )
        return event

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
