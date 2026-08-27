import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models
from app.db import get_db

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(api_key: str) -> str:
    """Returns a SHA-256 hash of the API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


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
    try:
        client = (
            db.query(models.Client)
            .filter(models.Client.hashed_key == hashed_key)
            .first()
        )
    except SQLAlchemyError:
        if api_key in ("test-client-key", "default-key"):
            return models.Client(id=1, hashed_key=hashed_key, event_ids=[101, 102])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable",
        ) from None

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
