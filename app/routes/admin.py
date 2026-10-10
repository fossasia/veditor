import json
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from rq import Worker
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from app.auth import (
    CurrentUser,
    get_current_user,
    lock_active_admins,
    require_admin,
)
from app.config import (
    EXCLUDED_SETTING_KEYS,
    PREVIEW_PRESETS,
    SYSTEM_SETTING_DEFINITIONS,
    SettingDefinition,
    settings,
)
from app.db import get_db
from app.queue import redis_conn
from app.storage import StorageBackend, get_storage_backend
from app.ui.templating import templates

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def _get_setting_options(
    key: str, defn: SettingDefinition | None
) -> list[schemas.SystemSettingOption]:
    if not defn:
        return []
    options = [
        schemas.SystemSettingOption(value=v, label=lbl) for v, lbl in defn.options
    ]
    if key == "default_preview_preset":
        existing_keys = {opt.value for opt in options}
        for k, p in settings.preview_presets.items():
            if k not in existing_keys:
                res = (
                    f" ({p.resolution[0]}x{p.resolution[1]})"
                    if hasattr(p, "resolution")
                    else ""
                )
                options.append(schemas.SystemSettingOption(value=k, label=f"{k}{res}"))
    return options


def _is_setting_overridden(
    row_value: str | None, defn: SettingDefinition | None
) -> bool:
    if row_value is None:
        return False
    if defn is None:
        return True
    try:
        return (
            float(row_value) != float(defn.default_value)
            if defn.value_type is float
            else row_value != str(defn.default_value)
        )
    except ValueError, TypeError:
        return True


def _get_all_settings_data(db: Session) -> list[schemas.SystemSettingRead]:
    db_rows = {s.key: s for s in db.query(models.SystemSetting).all()}
    results: list[schemas.SystemSettingRead] = []

    for def_key, defn in SYSTEM_SETTING_DEFINITIONS.items():
        row = db_rows.get(def_key)
        current_value = row.value if row else str(defn.default_value)
        is_overridden = _is_setting_overridden(row.value if row else None, defn)

        results.append(
            schemas.SystemSettingRead(
                key=def_key,
                title=defn.title,
                value=current_value,
                description=defn.description,
                updated_at=row.updated_at if row else None,
                is_overridden=is_overridden,
                default_value=str(defn.default_value),
                options=_get_setting_options(def_key, defn),
                input_type=defn.input_type,
                min_value=defn.min_value,
                max_value=defn.max_value,
                step=defn.step,
            )
        )

    return results


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    db_status = "Down"
    try:
        db.execute(text("SELECT 1"))
        db_status = "Healthy"
    except Exception:  # noqa: BLE001, S110
        pass

    redis_status = "Down"
    try:
        if redis_conn.ping():
            redis_status = "Healthy"
    except Exception:  # noqa: BLE001, S110
        pass

    light_workers = 0
    heavy_workers = 0
    try:
        all_workers = Worker.all(connection=redis_conn)
        for w in all_workers:
            queue_names = [q.name for q in w.queues]
            if "light" in queue_names:
                light_workers += 1
            if "heavy" in queue_names:
                heavy_workers += 1
    except Exception:  # noqa: BLE001, S110
        pass

    storage_status = "Available"
    free_bytes = 0
    total_bytes = 0
    used_bytes = 0
    try:
        free_bytes = storage.free_bytes()
        total_bytes = storage.total_bytes()
        used_bytes = total_bytes - free_bytes
    except OSError:
        storage_status = "Unavailable"

    settings_list = _get_all_settings_data(db)
    return templates.TemplateResponse(
        request,
        "admin_dashboard.html.jinja",
        {
            "db_status": db_status,
            "redis_status": redis_status,
            "light_workers": light_workers,
            "heavy_workers": heavy_workers,
            "storage_status": storage_status,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "settings": settings_list,
        },
    )


@router.get("/users")
def list_users(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    skip: int | None = Query(None, ge=0),
    search: str | None = Query(None),
    q: str | None = Query(None),
    email: str | None = Query(None),
    pending: bool | None = Query(None),
):
    search_term = (search or q or email or "").strip()
    query = db.query(models.User)
    pending_filter = pending is True or request.query_params.get("pending") in (
        "true",
        "1",
    )
    if pending_filter:
        query = query.filter(models.User.organizer_requested.is_(True))
    if search_term:
        escaped_search = (
            search_term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        query = query.filter(
            models.User.email.ilike(f"%{escaped_search}%", escape="\\")
        )

    total_users = query.count()
    total_pages = max(1, (total_users + limit - 1) // limit) if total_users > 0 else 1

    accept = request.headers.get("accept", "")
    format_param = request.query_params.get("format", "")
    wants_html = format_param == "html" or (
        "text/html" in accept
        and "application/json" not in accept
        and format_param != "json"
    )

    if skip is not None:
        offset = skip
        calculated_page = (skip // limit) + 1
    else:
        calculated_page = page
        offset = (page - 1) * limit

    if (
        wants_html
        and total_users > 0
        and (calculated_page > total_pages or offset >= total_users)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Page not found",
        )

    users = query.order_by(models.User.id.asc()).offset(offset).limit(limit).all()

    if wants_html:
        macro_row = db.query(
            func.count(models.User.id).label("total_users"),
            func.count(case((models.User.is_active.is_(True), 1))).label(
                "active_users"
            ),
            func.count(case((models.User.is_active.is_(False), 1))).label(
                "inactive_users"
            ),
            func.count(case((models.User.role == "admin", 1))).label("admin_users"),
            func.count(case((models.User.role == "organizer", 1))).label(
                "organizer_users"
            ),
            func.count(case((models.User.organizer_requested.is_(True), 1))).label(
                "pending_requests"
            ),
        ).first()

        macro_stats = {
            "total_users": 0,
            "active_users": 0,
            "inactive_users": 0,
            "admin_users": 0,
            "organizer_users": 0,
            "pending_requests": 0,
            **(
                {k: v or 0 for k, v in macro_row._asdict().items()} if macro_row else {}
            ),
        }

        pagination = {
            "page": calculated_page,
            "limit": limit,
            "total_items": total_users,
            "total_pages": total_pages,
            "has_prev": calculated_page > 1,
            "has_next": calculated_page < total_pages,
            "prev_page": calculated_page - 1,
            "next_page": calculated_page + 1,
            "start_item": offset + 1 if total_users > 0 and offset < total_users else 0,
            "end_item": min(offset + limit, total_users) if total_users > 0 else 0,
        }

        return templates.TemplateResponse(
            request,
            "admin_users.html.jinja",
            {
                "user": current_user,
                "users": users,
                "macro_stats": macro_stats,
                "pagination": pagination,
                "search": search_term,
                "pending_filter": pending_filter,
            },
        )

    return [schemas.UserRead.model_validate(u) for u in users]


@router.post("/users/{id}/promote", response_model=schemas.UserRead)
@router.post("/users/{id}/role", response_model=schemas.UserRead)
def promote_user(
    id: int,
    payload: schemas.UserPromoteRequest,
    db: Annotated[Session, Depends(get_db)],
):
    target = (
        db.query(models.User).filter(models.User.id == id).with_for_update().first()
    )
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    db.refresh(target)
    if target.role == "admin" and target.is_active and payload.role != "admin":
        admin_ids = lock_active_admins(db)
        db.refresh(target)
        if target.role == "admin" and target.is_active and len(admin_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote user; operation would leave zero active administrators",
            )

    target.role = payload.role
    if payload.role == "organizer" and target.organizer_requested:
        target.organizer_requested = False
        target.organizer_request_note = None
    db.commit()
    db.refresh(target)
    return target


@router.post("/users/{id}/activate", response_model=schemas.UserRead)
def activate_user(
    id: int,
    db: Annotated[Session, Depends(get_db)],
):
    target = db.query(models.User).filter(models.User.id == id).first()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    target.is_active = True
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


def _validate_system_setting(key: str, value: str) -> None:
    norm = key.strip().lower()
    if norm in EXCLUDED_SETTING_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Setting '{key}' is a protected credential or infrastructure parameter and cannot be modified dynamically.",
        )
    if norm not in SYSTEM_SETTING_DEFINITIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Setting '{key}' is not a recognized platform setting.",
        )

    defn = SYSTEM_SETTING_DEFINITIONS[norm]

    try:
        if norm == "detect_duration_tolerance_seconds":
            v = float(value)
            min_val = defn.min_value if defn.min_value is not None else 60.0
            max_val = defn.max_value if defn.max_value is not None else 1800.0
            if not math.isfinite(v) or not (min_val <= v <= max_val):
                raise ValueError
        elif norm == "loudness_target_lufs":
            v = float(value)
            if not math.isfinite(v) or not (-70.0 <= v <= -5.0):
                raise ValueError
        elif norm == "default_preview_preset":
            valid = set(settings.preview_presets.keys()) | set(PREVIEW_PRESETS.keys())
            if value not in valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"default_preview_preset must be one of: {sorted(valid)}",
                )
        elif norm == "default_transcode_preset":
            valid_transcode = ("480p", "720p", "1080p_default", "1440p")
            # ("4k_master",) commented out for now
            if value not in valid_transcode:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"default_transcode_preset must be one of: {list(valid_transcode)}",
                )
    except ValueError:
        detail = (
            f"detect_duration_tolerance_seconds must be a finite number between {defn.min_value} and {defn.max_value}"
            if norm == "detect_duration_tolerance_seconds"
            else "loudness_target_lufs must be a finite float between -70.0 and -5.0"
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


@router.get("/settings", response_class=HTMLResponse)
def get_settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    status_msg: str | None = Query(None, alias="status"),
    error_msg: str | None = Query(None, alias="error"),
):
    settings_list = _get_all_settings_data(db)
    return templates.TemplateResponse(
        request,
        "admin_settings.html.jinja",
        {
            "settings": settings_list,
            "status_msg": status_msg,
            "error_msg": error_msg,
        },
    )


PROCESSING_STATUSES = (
    "detecting",
    "cutting",
    "generating_previews",
    "assembling",
    "transcoding",
    "uploading",
)
PENDING_STATUSES = (
    "pending_approval",
    "pending_bounds",
    "needs_work",
    "pending_intro_outro",
)


@router.get("/events", response_class=HTMLResponse)
def admin_events(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    total_events = db.query(func.count(models.Event.id)).scalar() or 0
    macro_row = db.query(
        func.count(models.Talk.id).label("total_talks"),
        func.count(case((models.Talk.status == "done", 1))).label("total_done"),
        func.count(case((models.Talk.status.in_(["broken", "rejected"]), 1))).label(
            "total_broken"
        ),
        func.count(case((models.Talk.status.in_(PROCESSING_STATUSES), 1))).label(
            "total_processing"
        ),
    ).first()

    macro_stats = {
        "total_events": total_events,
        "total_talks": 0,
        "total_done": 0,
        "total_processing": 0,
        "total_broken": 0,
        **({k: v or 0 for k, v in macro_row._asdict().items()} if macro_row else {}),
    }

    total_pages = max(1, (total_events + limit - 1) // limit) if total_events > 0 else 1
    if page > total_pages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Page not found",
        )
    offset = (page - 1) * limit

    rows = (
        db.query(
            models.Event.id,
            models.Event.name,
            models.Event.created_by_user_id,
            models.User.email.label("creator_email"),
            func.count(models.Talk.id).label("total_talks"),
            func.count(case((models.Talk.status == "done", 1))).label("done_count"),
            func.count(case((models.Talk.status.in_(["broken", "rejected"]), 1))).label(
                "broken_count"
            ),
            func.count(case((models.Talk.status.in_(PROCESSING_STATUSES), 1))).label(
                "processing_count"
            ),
            func.count(case((models.Talk.status.in_(PENDING_STATUSES), 1))).label(
                "pending_count"
            ),
            func.count(case((models.Talk.status == "preview", 1))).label(
                "preview_count"
            ),
            func.count(case((models.Talk.status == "waiting_for_files", 1))).label(
                "waiting_count"
            ),
        )
        .outerjoin(models.Talk, models.Talk.event_id == models.Event.id)
        .outerjoin(models.User, models.Event.created_by_user_id == models.User.id)
        .group_by(
            models.Event.id,
            models.Event.name,
            models.Event.created_by_user_id,
            models.User.email,
        )
        .order_by(models.Event.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    events = [
        {
            **r._asdict(),
            "progress_pct": round(((r.done_count or 0) / r.total_talks) * 100, 1)
            if r.total_talks
            else 0,
        }
        for r in rows
    ]

    pagination = {
        "page": page,
        "limit": limit,
        "total_items": total_events,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start_item": offset + 1 if total_events > 0 and offset < total_events else 0,
        "end_item": min(offset + limit, total_events) if total_events > 0 else 0,
    }

    return templates.TemplateResponse(
        request,
        "admin_events.html.jinja",
        {
            "events": events,
            "macro_stats": macro_stats,
            "pagination": pagination,
        },
    )


@router.get("/api/settings", response_model=list[schemas.SystemSettingRead])
def list_api_settings(db: Annotated[Session, Depends(get_db)]):
    return _get_all_settings_data(db)


def _upsert_setting(
    db: Session, key: str, value: str, description: str | None = None
) -> models.SystemSetting:
    key = key.strip()
    value = value.strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Setting key cannot be empty",
        )
    if len(key) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Setting key cannot exceed 255 characters",
        )
    _validate_system_setting(key, value)
    key = key.lower()

    setting = db.get(models.SystemSetting, key)
    desc = (description.strip() if description else None) or (
        SYSTEM_SETTING_DEFINITIONS[key].description
        if key in SYSTEM_SETTING_DEFINITIONS
        else None
    )
    if not setting:
        setting = models.SystemSetting(
            key=key, value=value, description=desc, updated_at=datetime.now(UTC)
        )
        db.add(setting)
    else:
        setting.value = value
        if description:
            setting.description = desc
        setting.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(setting)
    return setting


@router.post("/settings")
async def update_setting(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    if "application/json" in request.headers.get("content-type", ""):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError
        except TypeError, ValueError, json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="JSON payload must be an object",
            )

        setting = _upsert_setting(
            db,
            str(payload.get("key") or ""),
            str(payload.get("value") or ""),
            payload.get("description"),
        )
        defn = SYSTEM_SETTING_DEFINITIONS.get(setting.key)
        return schemas.SystemSettingRead(
            key=setting.key,
            title=defn.title if defn else None,
            value=setting.value,
            description=setting.description,
            updated_at=setting.updated_at,
            is_overridden=_is_setting_overridden(setting.value, defn),
            default_value=str(defn.default_value) if defn else None,
            options=_get_setting_options(setting.key, defn),
            input_type=defn.input_type if defn else "select",
            min_value=defn.min_value if defn else None,
            max_value=defn.max_value if defn else None,
            step=defn.step if defn else None,
        )

    form = await request.form()
    if form.get("action") == "reset_all":
        db.query(models.SystemSetting).filter(
            models.SystemSetting.key.in_(SYSTEM_SETTING_DEFINITIONS.keys())
        ).delete(synchronize_session=False)
        db.commit()
        return RedirectResponse(
            url="/admin/settings?status=reset",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if "key" in form:
        _upsert_setting(
            db,
            str(form.get("key") or ""),
            str(form.get("value") or ""),
            form.get("description"),
        )
        return RedirectResponse(
            url="/admin/settings?status=saved",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    for def_key, defn in SYSTEM_SETTING_DEFINITIONS.items():
        if def_key in form:
            val = str(form.get(def_key)).strip()
            _validate_system_setting(def_key, val)
            setting = db.get(models.SystemSetting, def_key)
            try:
                is_default = (
                    float(val) == float(defn.default_value)
                    if defn.value_type is float
                    else val == str(defn.default_value)
                )
            except ValueError, TypeError:
                is_default = False

            if is_default:
                if setting:
                    db.delete(setting)
            elif setting:
                setting.value = val
                setting.updated_at = datetime.now(UTC)
            else:
                db.add(
                    models.SystemSetting(
                        key=def_key,
                        value=val,
                        description=defn.description,
                        updated_at=datetime.now(UTC),
                    )
                )

    db.commit()
    return RedirectResponse(
        url="/admin/settings?status=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/settings/{key}/reset")
async def reset_setting(
    key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    key = key.strip().lower()
    setting = db.get(models.SystemSetting, key)
    if setting:
        db.delete(setting)
        db.commit()

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return {"status": "ok", "key": key}

    return RedirectResponse(
        url="/admin/settings?status=reset",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def admin_event_detail(
    request: Request,
    event_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    event = (
        db.query(models.Event)
        .options(selectinload(models.Event.created_by_user))
        .filter(models.Event.id == event_id)
        .first()
    )
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    talks = (
        db.query(models.Talk)
        .filter(models.Talk.event_id == event_id)
        .order_by(models.Talk.start.asc(), models.Talk.id.asc())
        .all()
    )

    counts = Counter(t.status for t in talks)
    total = len(talks)
    done_count = counts["done"]
    progress_pct = round((done_count / total) * 100, 1) if total > 0 else 0

    event_stats = {
        "total": total,
        "done": done_count,
        "broken": counts["broken"] + counts["rejected"],
        "processing": sum(counts[s] for s in PROCESSING_STATUSES),
        "pending": sum(counts[s] for s in PENDING_STATUSES),
        "preview": counts["preview"],
        "waiting": counts["waiting_for_files"],
    }

    return templates.TemplateResponse(
        request,
        "admin_event_detail.html.jinja",
        {
            "event": event,
            "talks": talks,
            "event_stats": event_stats,
            "progress_pct": progress_pct,
        },
    )
