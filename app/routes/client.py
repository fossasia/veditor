import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_client
from app.db import get_db

router = APIRouter(
    tags=["client"],
)


@router.post(
    "/client/webhook",
    response_model=schemas.WebhookRegisterResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/clients/webhook",
    response_model=schemas.WebhookRegisterResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def register_webhook(
    payload: schemas.WebhookRegisterRequest,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Register or update the client's outbound notification URL and shared secret.
    If secret is not provided in payload, a cryptographically secure random secret is generated.
    """
    secret = (
        payload.secret.strip()
        if payload.secret and payload.secret.strip()
        else secrets.token_urlsafe(32)
    )

    client.webhook_url = payload.url
    client.webhook_secret = secret
    db.commit()
    db.refresh(client)

    return schemas.WebhookRegisterResponse(
        status="registered",
        url=client.webhook_url,
        secret=client.webhook_secret,
    )


@router.get(
    "/client/webhook",
    response_model=schemas.WebhookInfoResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/clients/webhook",
    response_model=schemas.WebhookInfoResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def get_webhook(
    client: Annotated[models.Client, Depends(get_client)],
):
    """Retrieve the client's currently registered webhook info."""
    return schemas.WebhookInfoResponse(
        url=client.webhook_url,
        has_secret=bool(client.webhook_secret),
    )


@router.delete(
    "/client/webhook",
    status_code=status.HTTP_204_NO_CONTENT,
)
@router.delete(
    "/clients/webhook",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
def delete_webhook(
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """Clear the client's registered webhook URL and secret."""
    client.webhook_url = None
    client.webhook_secret = None
    db.commit()
