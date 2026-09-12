from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import (
    CurrentUser,
    get_current_user,
    lock_active_admins,
    require_admin,
)
from app.db import get_db

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/users", response_model=list[schemas.UserRead])
def list_users(
    db: Annotated[Session, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return (
        db.query(models.User)
        .order_by(models.User.id.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post("/users/{id}/promote", response_model=schemas.UserRead)
def promote_user(
    id: int,
    payload: schemas.UserPromoteRequest,
    db: Annotated[Session, Depends(get_db)],
):
    target = db.query(models.User).filter(models.User.id == id).first()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if target.role == "admin" and target.is_active and payload.role != "admin":
        admin_ids = lock_active_admins(db)
        db.refresh(target)
        if target.role == "admin" and target.is_active and len(admin_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote user; operation would leave zero active administrators",
            )

    target.role = payload.role
    db.commit()
    db.refresh(target)
    return target


@router.post("/users/{id}/deactivate", response_model=schemas.UserRead)
def deactivate_user(
    id: int,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if current_user.user_id is not None and current_user.user_id == id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account",
        )

    target = db.query(models.User).filter(models.User.id == id).first()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if target.role == "admin" and target.is_active:
        admin_ids = lock_active_admins(db)
        db.refresh(target)
        if target.role == "admin" and target.is_active and len(admin_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the last active administrator",
            )

    target.is_active = False
    db.commit()
    db.refresh(target)
    return target
