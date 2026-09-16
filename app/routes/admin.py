from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from app.auth import (
    CurrentUser,
    get_current_user,
    lock_active_admins,
    require_admin,
)
from app.db import get_db
from app.runtime_settings import RUNTIME_SETTINGS, resolve_setting
from app.ui.templating import templates

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


def _render_settings(
    request: Request,
    db: Session,
    *,
    notice: str | None = None,
    errors: dict[str, str] | None = None,
    submitted: dict[str, str] | None = None,
    status_code: int = status.HTTP_200_OK,
):
    overrides = {
        row.key: row
        for row in db.query(models.SystemSetting)
        .options(selectinload(models.SystemSetting.updated_by_user))
        .filter(models.SystemSetting.key.in_(RUNTIME_SETTINGS))
    }
    items = []
    for spec in RUNTIME_SETTINGS.values():
        row = overrides.get(spec.key)
        value, overridden = resolve_setting(spec, row)
        items.append(
            {
                "spec": spec,
                "value": value,
                "default": spec.default(),
                "overridden": overridden,
                "row": row,
            }
        )
    return templates.TemplateResponse(
        request,
        "admin_settings.html.jinja",
        {
            "items": items,
            "notice": notice,
            "errors": errors or {},
            "submitted": submitted or {},
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    saved: str | None = None,
    reset: str | None = None,
):
    notice = None
    if saved in RUNTIME_SETTINGS:
        notice = f"Saved {RUNTIME_SETTINGS[saved].label}."
    elif reset in RUNTIME_SETTINGS:
        notice = f"Reset {RUNTIME_SETTINGS[reset].label} to its default."
    return _render_settings(request, db, notice=notice)


@router.post("/settings/{key}", response_class=HTMLResponse)
def update_setting(
    key: str,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    value: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
):
    spec = RUNTIME_SETTINGS.get(key)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown setting",
        )

    if action == "reset":
        db.query(models.SystemSetting).filter(models.SystemSetting.key == key).delete()
        db.commit()
        return RedirectResponse(
            url=f"/admin/settings?reset={key}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if action != "save":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported action",
        )

    try:
        parsed = spec.parse(value)
    except ValueError as exc:
        return _render_settings(
            request,
            db,
            errors={key: f"{spec.label} {exc}."},
            submitted={key: value},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Upsert so concurrent saves of the same key cannot collide on the primary key.
    values = {
        "value": parsed,
        "description": spec.description,
        "updated_at": datetime.now(UTC),
        "updated_by_user_id": current_user.user_id,
    }
    stmt = insert(models.SystemSetting).values(key=key, **values)
    db.execute(stmt.on_conflict_do_update(index_elements=["key"], set_=values))
    db.commit()
    return RedirectResponse(
        url=f"/admin/settings?saved={key}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
