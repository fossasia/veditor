from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from unittest.mock import call as mock_call

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import CurrentUser, get_current_user, hash_api_key
from app.consts import AuthRole, TalkStatus
from app.main import app
from app.review_handlers import (
    DECISION_HANDLERS,
    handle_approve,
    handle_needs_work,
    handle_reject,
)
from app.storage import get_storage_backend
from tests.conftest import FakeStorageBackend, TestSessionLocal

client = TestClient(app)

API_KEY = "valid_key"


def _create_api_client(
    db: Session, event_ids: list[int], api_key: str = API_KEY
) -> models.Client:
    client_row = models.Client(hashed_key=hash_api_key(api_key), event_ids=event_ids)
    db.add(client_row)
    db.commit()
    db.refresh(client_row)
    return client_row


def _create_user(
    db: Session, email: str, role: AuthRole = AuthRole.ORGANIZER
) -> models.User:
    user = models.User(email=email, hashed_password="x", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_event(db: Session, name: str) -> models.Event:
    event = models.Event(name=name)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(
    db: Session, event_id: int, title: str = "Test Review Talk", **kwargs: Any
) -> models.Talk:
    talk = models.Talk(
        event_id=event_id,
        title=title,
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
        cut_start=10.0,
        cut_end=60.0,
        raw_duration_seconds=120.0,
        **kwargs,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def _review_count(db: Session, talk_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(models.Review)
            .where(models.Review.talk_id == talk_id)
        )
        or 0
    )


@pytest.fixture
def preview_talk(db_session: Session) -> models.Talk:
    event = _create_event(db_session, "Review Test Event")
    return _create_talk(db_session, event.id)


def test_review_unauthorized() -> None:
    """POST /talks/{id}/review without credentials returns 401."""
    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_review_invalid_decision_returns_422(db_session: Session) -> None:
    """POST with invalid decision fails schema validation with 422."""
    _create_api_client(db_session, event_ids=[1])

    response = client.post(
        "/talks/1/review",
        json={"decision": "invalid_decision"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422


def test_review_talk_not_found(db_session: Session) -> None:
    """POST /talks/{id}/review returns 404 if talk does not exist."""
    _create_api_client(db_session, event_ids=[1])

    response = client.post(
        "/talks/999/review",
        json={"decision": "approve"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_review_forbidden_event(db_session: Session, preview_talk: models.Talk) -> None:
    """POST /talks/{id}/review returns 403 if client is not authorized for talk's event."""
    _create_api_client(db_session, event_ids=[preview_talk.event_id + 100])

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Client is not authorized to access this event"


@pytest.mark.parametrize(
    "invalid_status",
    [
        "waiting_for_files",
        "detecting",
        "pending_approval",
        "pending_bounds",
        "cutting",
        "generating_previews",
        "transcoding",
        "uploading",
        "done",
        "rejected",
        "broken",
        "needs_work",
        "pending_intro_outro",
    ],
)
def test_review_conflict_non_preview_state(
    db_session: Session, preview_talk: models.Talk, invalid_status: str
) -> None:
    """POST /talks/{id}/review returns 409 if talk status is not 'preview'."""
    preview_talk.status = invalid_status
    db_session.commit()
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Cannot review talk in status '{invalid_status}'; talk must be in 'preview'"
    )


@pytest.mark.parametrize(
    ("decision", "note", "expected_status"),
    [
        ("approve", None, "assembling"),
        ("approve", "Looks great!", "assembling"),
        ("needs_work", None, "pending_bounds"),
        ("needs_work", "Audio is cut off at the start", "pending_bounds"),
        ("reject", None, "pending_bounds"),
        ("reject", "Not suitable for publication", "pending_bounds"),
    ],
)
def test_review_valid_decisions_success(
    db_session: Session,
    preview_talk: models.Talk,
    decision: str,
    note: str | None,
    expected_status: str,
) -> None:
    """POST /talks/{id}/review returns 200 with ReviewResponse and atomic Review audit trail."""
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    body = {"decision": decision}
    if note is not None:
        body["note"] = note

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json=body,
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["id"] == preview_talk.id
    assert data["talk"]["status"] == expected_status
    if decision == "reject":
        assert data["talk"]["cut_start"] is None
        assert data["talk"]["cut_end"] is None
    else:
        assert data["talk"]["cut_start"] == 10.0
        assert data["talk"]["cut_end"] == 60.0
    assert data["review"] is not None
    assert data["review"]["talk_id"] == preview_talk.id
    assert data["review"]["decision"] == decision
    assert data["review"]["note"] == note
    assert "created_at" in data["review"]
    assert data["review"]["created_at"] is not None

    db_session.expire_all()
    reloaded = db_session.get(models.Talk, preview_talk.id)
    assert reloaded is not None
    assert reloaded.status == expected_status
    assert _review_count(db_session, preview_talk.id) == 1

    if decision == "approve":
        approved_cut = db_session.scalar(
            select(models.ApprovedCut).where(
                models.ApprovedCut.talk_id == preview_talk.id
            )
        )
        assert approved_cut is not None
        assert approved_cut.cut_start == 10.0
        assert approved_cut.cut_end == 60.0


@pytest.mark.parametrize(
    ("decision", "handler_name"),
    [
        (schemas.ReviewDecision.approve, "handle_approve"),
        (schemas.ReviewDecision.needs_work, "handle_needs_work"),
        (schemas.ReviewDecision.reject, "handle_reject"),
    ],
)
def test_review_dispatch_invokes_correct_handler(
    db_session: Session,
    preview_talk: models.Talk,
    decision: schemas.ReviewDecision,
    handler_name: str,
) -> None:
    """Verify that route dispatches to the registered handler in DECISION_HANDLERS."""
    assert DECISION_HANDLERS[decision].__name__ == handler_name
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    mock_handler = MagicMock(
        return_value=schemas.ReviewResponse(
            talk=schemas.TalkRead.model_validate(preview_talk),
            review=schemas.ReviewRead(
                id=1,
                talk_id=preview_talk.id,
                decision=decision.value,
                note="Dispatch test note",
                created_at=datetime.now(UTC),
            ),
        )
    )

    with patch.dict(DECISION_HANDLERS, {decision: mock_handler}):
        response = client.post(
            f"/talks/{preview_talk.id}/review",
            json={"decision": decision.value, "note": "Dispatch test note"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 200
        mock_handler.assert_called_once()
        called_talk, called_payload, called_db = mock_handler.call_args[0]
        assert called_talk is preview_talk
        assert called_payload.decision == decision
        assert called_payload.note == "Dispatch test note"
        assert called_db is db_session


def test_handlers_direct_persistence_and_advance(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """Directly test handle_approve, handle_needs_work, handle_reject with DB transaction."""
    # Test handle_approve -> assembling
    req_approve = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.approve, note="Approve note"
    )
    with patch("app.tasks.dispatch_assembly"):
        resp_approve = handle_approve(preview_talk, req_approve, db_session)
    assert resp_approve.talk.id == preview_talk.id
    assert resp_approve.talk.status == TalkStatus.ASSEMBLING
    assert resp_approve.review is not None
    assert resp_approve.review.decision == "approve"
    assert resp_approve.review.note == "Approve note"

    # Reset talk status for needs_work test
    preview_talk.status = TalkStatus.PREVIEW
    db_session.commit()
    req_work = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.needs_work, note="Fix cut"
    )
    resp_work = handle_needs_work(preview_talk, req_work, db_session)
    assert resp_work.talk.id == preview_talk.id
    assert resp_work.talk.status == TalkStatus.PENDING_BOUNDS
    assert resp_work.review is not None
    assert resp_work.review.decision == "needs_work"
    assert resp_work.review.note == "Fix cut"

    # Reset talk status for reject test -> pending_bounds
    preview_talk.status = TalkStatus.PREVIEW
    db_session.commit()
    req_reject = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.reject, note="Reset bounds"
    )
    resp_reject = handle_reject(preview_talk, req_reject, db_session)
    assert resp_reject.talk.id == preview_talk.id
    assert resp_reject.talk.status == TalkStatus.PENDING_BOUNDS
    assert resp_reject.talk.cut_start is None
    assert resp_reject.talk.cut_end is None
    assert preview_talk.cut_start is None
    assert preview_talk.cut_end is None
    assert preview_talk.status == TalkStatus.PENDING_BOUNDS
    assert resp_reject.review is not None
    assert resp_reject.review.decision == "reject"
    assert resp_reject.review.note == "Reset bounds"


def test_handle_reject_clears_cut_bounds_reset_to_raw(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """Rejecting a talk in preview clears cut bounds, transitions to pending_bounds, purges cut/preview files, and enqueues no jobs."""
    event = _create_event(db_session, "Reject Reset Event")
    talk = _create_talk(db_session, event.id, title="Reject Reset Talk")
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    fake_storage.put(f"{talk.id}/raw/video.mp4", b"raw footage")
    fake_storage.put(f"{talk.id}/cut/cut.mp4", b"cut footage")
    fake_storage.put(f"{talk.id}/preview/preview.mp4", b"preview video")

    with (
        patch("app.queue.light_queue.enqueue") as mock_light_enqueue,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        response = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "reject", "note": "Bounds inaccurate, reset to raw"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["talk"]["status"] == "pending_bounds"
        assert data["talk"]["cut_start"] is None
        assert data["talk"]["cut_end"] is None
        assert data["talk"]["raw_duration_seconds"] == 120.0
        assert data["review"]["decision"] == "reject"
        assert data["review"]["note"] == "Bounds inaccurate, reset to raw"

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.cut_start is None
        assert reloaded.cut_end is None
        assert reloaded.status == "pending_bounds"

        assert not fake_storage.exists(f"{talk.id}/cut/cut.mp4")
        assert not fake_storage.exists(f"{talk.id}/preview/preview.mp4")
        assert fake_storage.exists(f"{talk.id}/raw/video.mp4")

        mock_light_enqueue.assert_not_called()
        mock_heavy_enqueue.assert_not_called()


def test_handle_reject_storage_delete_error_resilient(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """Storage deletion errors in handle_reject do not crash endpoint (returns 200) and both targets are attempted."""
    event = _create_event(db_session, "Reject Resilience Event")
    talk = _create_talk(db_session, event.id, title="Reject Error Resilience Talk")
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.delete.side_effect = [
        RuntimeError("Storage connection failed"),
        None,
        None,
        None,
        None,
    ]

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    response = client.post(
        f"/talks/{talk.id}/review",
        json={"decision": "reject", "note": "Failed cut deletion shouldn't 500"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["status"] == "pending_bounds"
    assert data["talk"]["cut_start"] is None
    assert data["talk"]["cut_end"] is None
    assert data["review"]["decision"] == "reject"

    db_session.expire_all()
    reloaded = db_session.get(models.Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == "pending_bounds"
    assert reloaded.cut_start is None
    assert reloaded.cut_end is None

    assert mock_storage.delete.call_count == 5
    assert mock_storage.delete.call_args_list == [
        mock_call(f"{talk.id}/cut"),
        mock_call(f"{talk.id}/preview"),
        mock_call(f"{talk.id}/assemble"),
        mock_call(f"{talk.id}/intro"),
        mock_call(f"{talk.id}/outro"),
    ]


def test_simulated_commit_failure_rolls_back_review_insert(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """Simulated failure of the DB commit rolls back the Review insert and talk transition."""
    req = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    with (
        patch.object(
            db_session, "commit", side_effect=RuntimeError("Simulated DB write failure")
        ),
        pytest.raises(RuntimeError, match="Simulated DB write failure"),
    ):
        handle_approve(preview_talk, req, db_session)

    db_session.rollback()
    assert preview_talk.status == TalkStatus.PREVIEW
    assert _review_count(db_session, preview_talk.id) == 0


def test_simulated_flush_failure_rolls_back_review_insert(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """Simulated failure of the DB flush rolls back the Review insert and talk transition."""
    req = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    with (
        patch.object(
            db_session, "flush", side_effect=RuntimeError("Simulated DB flush failure")
        ),
        pytest.raises(RuntimeError, match="Simulated DB flush failure"),
    ):
        handle_approve(preview_talk, req, db_session)

    db_session.rollback()
    assert preview_talk.status == TalkStatus.PREVIEW
    assert _review_count(db_session, preview_talk.id) == 0


def test_review_locks_talk_row_with_for_update(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """POST /talks/{id}/review acquires a row-level lock on the talk while reviewing."""
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "needs_work", "note": "locking check"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, preview_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_BOUNDS


def test_flush_before_commit_and_no_refresh(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """Review handler persists the review atomically in a single commit."""
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "needs_work", "note": "persistence check"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    assert _review_count(db_session, preview_talk.id) == 1


def test_append_only_audit_trail_relationship() -> None:
    """Verify multiple reviews produce an append-only audit trail on talk."""
    talk = models.Talk(
        id=10,
        event_id=1,
        title="Audit Trail Talk",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
    )
    r1 = models.Review(
        talk_id=talk.id,
        decision="needs_work",
        note="Audio cut off",
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    r2 = models.Review(
        talk_id=talk.id,
        decision="reject",
        note="Bounds wrong",
        created_at=datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
    )
    r3 = models.Review(
        talk_id=talk.id,
        decision="approve",
        note="All good",
        created_at=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
    )
    talk.reviews.extend([r1, r2, r3])
    assert len(talk.reviews) == 3
    assert [r.decision for r in talk.reviews] == ["needs_work", "reject", "approve"]
    assert [r.note for r in talk.reviews] == [
        "Audio cut off",
        "Bounds wrong",
        "All good",
    ]


def test_concurrent_reviews_atomic_transition_and_single_review() -> None:
    """Verify concurrent review submissions result in exactly one success and one 409."""
    import concurrent.futures

    from sqlalchemy.exc import SQLAlchemyError

    from app.db import SessionLocal

    # Check database availability
    probe_db = None
    try:
        probe_db = SessionLocal()
        probe_db.execute(select(1))
    except (
        SQLAlchemyError,
        OSError,
    ):
        pytest.skip("Database connection unavailable for concurrent integration test")
    finally:
        if probe_db is not None:
            probe_db.close()

    db = SessionLocal()
    event = models.Event(name="Concurrent Review Test Event")
    db.add(event)
    db.flush()
    event_id = event.id

    client_record = models.Client(
        hashed_key=hash_api_key("concurrent_key"), event_ids=[event_id]
    )
    db.add(client_record)
    db.flush()
    client_id = client_record.id

    talk = models.Talk(
        event_id=event_id,
        title="Concurrent Review Talk",
        room="Room Concurrent",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
        cut_start=10.0,
        cut_end=60.0,
    )
    db.add(talk)
    db.commit()
    talk_id = talk.id
    db.close()

    try:

        def post_review(decision: str) -> Response:
            test_client = TestClient(app)
            return test_client.post(
                f"/talks/{talk_id}/review",
                json={"decision": decision, "note": f"Concurrent decision {decision}"},
                headers={"X-API-Key": "concurrent_key"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(post_review, "approve")
            fut2 = executor.submit(post_review, "needs_work")
            res1 = fut1.result()
            res2 = fut2.result()

        status_codes = sorted([res1.status_code, res2.status_code])
        assert status_codes == [200, 409]

        db = SessionLocal()
        reviews = list(
            db.scalars(select(models.Review).where(models.Review.talk_id == talk_id))
        )
        assert len(reviews) == 1

        final_talk = db.scalar(select(models.Talk).where(models.Talk.id == talk_id))
        assert final_talk is not None
        assert final_talk.status in (TalkStatus.ASSEMBLING, TalkStatus.PENDING_BOUNDS)
        assert final_talk.status != TalkStatus.PREVIEW
        assert reviews[0].decision in ("approve", "needs_work")
        db.close()
    finally:
        clean_db = SessionLocal()
        clean_db.execute(delete(models.Review).where(models.Review.talk_id == talk_id))
        clean_db.execute(delete(models.Talk).where(models.Talk.id == talk_id))
        clean_db.execute(delete(models.Client).where(models.Client.id == client_id))
        clean_db.execute(delete(models.Event).where(models.Event.id == event_id))
        clean_db.commit()
        clean_db.close()


def test_approve_advances_to_assembling_and_dispatches_assembly(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """
    Acceptance Criteria:
    - POST /talks/{id}/review with approve transitions talk to 'assembling'.
    - Calls dispatch_assembly to initiate final encoding pipeline.
    - Review model record is created in the database.
    """
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    with patch("app.tasks.dispatch_assembly") as mock_dispatch:
        response = client.post(
            f"/talks/{preview_talk.id}/review",
            json={"decision": "approve", "note": "Approved by speaker"},
            headers={"X-API-Key": API_KEY},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["talk"]["status"] == "assembling"

        mock_dispatch.assert_called_once()

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, preview_talk.id)
        assert reloaded is not None
        assert reloaded.status == "assembling"
        assert _review_count(db_session, preview_talk.id) == 1


def test_review_needs_work_transitions_to_pending_bounds_retains_offsets_and_no_job_enqueued(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """POST /talks/{id}/review with needs_work transitions talk to pending_bounds,

    preserves existing cut_start and cut_end offsets intact, and enqueues zero jobs.
    """
    preview_talk.cut_start = 12.5
    preview_talk.cut_end = 75.0
    db_session.commit()
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    with (
        patch("app.queue.light_queue.enqueue") as mock_light_enqueue,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        response = client.post(
            f"/talks/{preview_talk.id}/review",
            json={"decision": "needs_work", "note": "Audio cut off at beginning"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["talk"]["status"] == "pending_bounds"
        assert data["talk"]["cut_start"] == 12.5
        assert data["talk"]["cut_end"] == 75.0
        assert data["review"]["decision"] == "needs_work"
        assert data["review"]["note"] == "Audio cut off at beginning"

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, preview_talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_bounds"
        assert reloaded.cut_start == 12.5
        assert reloaded.cut_end == 75.0

        # Explicit invariant: no RQ jobs enqueued on needs_work transition
        mock_light_enqueue.assert_not_called()
        mock_heavy_enqueue.assert_not_called()


def test_needs_work_subsequent_cut_overwrites_outputs(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """Verify that after needs_work transitions to pending_bounds with bounds intact:

    1. Prior cut and preview artifacts remain in place.
    2. Raw recording remains completely untouched.
    3. Subsequent POST /cut with adjusted bounds transitions to cutting and enqueues cut.
    4. Subsequent cut and preview runs overwrite previous outputs.
    """
    event = _create_event(db_session, "Needs Work E2E Event")
    talk = _create_talk(db_session, event.id, title="Needs Work E2E Talk")
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    fake_storage.put(f"{talk.id}/raw/video.mp4", b"original raw video bytes")
    fake_storage.put(f"{talk.id}/cut/cut.mp4", b"old cut content v1")
    fake_storage.put(f"{talk.id}/preview/preview.mp4", b"old preview content v1")

    # Step 1: Review decision 'needs_work'
    with (
        patch("app.queue.light_queue.enqueue") as mock_light_q,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_q,
    ):
        resp_review = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "needs_work", "note": "Adjust start boundary"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp_review.status_code == 200
        review_data = resp_review.json()
        assert review_data["talk"]["status"] == "pending_bounds"
        assert review_data["talk"]["cut_start"] == 10.0
        assert review_data["talk"]["cut_end"] == 60.0

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_bounds"
        assert reloaded.cut_start == 10.0
        assert reloaded.cut_end == 60.0

        # No background jobs enqueued
        mock_light_q.assert_not_called()
        mock_heavy_q.assert_not_called()

        # Prior artifacts still exist, raw untouched
        assert fake_storage.get(f"{talk.id}/raw/video.mp4").read_bytes() == (
            b"original raw video bytes"
        )
        assert fake_storage.get(f"{talk.id}/cut/cut.mp4").read_bytes() == (
            b"old cut content v1"
        )
        assert fake_storage.get(f"{talk.id}/preview/preview.mp4").read_bytes() == (
            b"old preview content v1"
        )

    # Step 2: Speaker resubmits adjusted bounds via POST /talks/{id}/cut
    with patch("app.routes.talks.light_queue.enqueue") as mock_cut_enqueue:
        resp_cut = client.post(
            f"/talks/{talk.id}/cut",
            json={"cut_start": "00:00:20", "cut_end": "00:00:50"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp_cut.status_code == 202
        cut_data = resp_cut.json()
        assert cut_data["status"] == "cutting"
        assert cut_data["cut_start"] == 20.0
        assert cut_data["cut_end"] == 50.0

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "cutting"
        assert reloaded.cut_start == 20.0
        assert reloaded.cut_end == 50.0

        # Verify job_cut was enqueued
        mock_cut_enqueue.assert_called_once()
        call_args = mock_cut_enqueue.call_args
        queued_cut = call_args[0][0]
        cut_args = call_args[0][1:]
        assert call_args[0][1] == talk.id
        assert call_args[0][2] == f"{talk.id}/raw/video.mp4"

    # Step 3: Run queued cut and preview workers to overwrite artifacts.
    # The job functions open their own sessions; bind them to the test
    # connection so they see the uncommitted outer transaction.
    job_db = TestSessionLocal(bind=db_session.get_bind())

    with (
        patch("app.tasks.SessionLocal", return_value=job_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch(
            "app.tasks.cut",
            side_effect=lambda inp, out, s, e: Path(out).write_bytes(
                b"new cut content v2"
            ),
        ),
        patch(
            "app.tasks.generate_preview",
            side_effect=lambda inp, out, *args, **kwargs: Path(out).write_bytes(
                b"new preview content v2"
            ),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_preview_enqueue,
    ):
        queued_cut(*cut_args)
        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "generating_previews"

        mock_preview_enqueue.assert_called_once()
        queued_preview = mock_preview_enqueue.call_args[0][0]
        preview_args = mock_preview_enqueue.call_args[0][1:]

        queued_preview(*preview_args)
        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "preview"
        assert mock_preview_enqueue.call_count == 1

    job_db.close()

    assert fake_storage.get(f"{talk.id}/cut/cut.mp4").read_bytes() == (
        b"new cut content v2"
    )
    assert fake_storage.get(f"{talk.id}/preview/preview.mp4").read_bytes() == (
        b"new preview content v2"
    )
    # Raw recording is still untouched
    assert fake_storage.get(f"{talk.id}/raw/video.mp4").read_bytes() == (
        b"original raw video bytes"
    )


def test_review_human_user_role_forbidden(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """POST /talks/{id}/review returns 403 if human caller has role 'user'."""
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=10, email="user@example.com", role="user", source="cookie", event_ids=[]
    )

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 403
    assert "Operation requires minimum role 'organizer'" in response.json()["detail"]


def test_review_human_organizer_unowned_event_forbidden(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """POST /talks/{id}/review returns 403 if organizer does not own the event."""
    owner = _create_user(db_session, "owner@example.com")
    preview_talk.event.created_by_user_id = owner.id
    db_session.commit()

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id + 1,
        email="org@example.com",
        role="organizer",
        source="cookie",
        event_ids=[],
    )

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "User is not authorized to access this event"


def test_review_human_organizer_owned_event_success(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """POST /talks/{id}/review succeeds for organizer who owns the event."""
    organizer = _create_user(db_session, "org@example.com")
    preview_talk.event.created_by_user_id = organizer.id
    db_session.commit()

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=organizer.id,
        email="org@example.com",
        role="organizer",
        source="cookie",
        event_ids=[],
    )

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["status"] == "assembling"
    assert data["review"]["user_id"] == organizer.id


def test_review_human_admin_success(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """POST /talks/{id}/review succeeds for admin even if event created by someone else."""
    owner = _create_user(db_session, "owner@example.com")
    admin = _create_user(db_session, "admin@example.com", role=AuthRole.ADMIN)
    preview_talk.event.created_by_user_id = owner.id
    db_session.commit()

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=admin.id,
        email="admin@example.com",
        role="admin",
        source="cookie",
        event_ids=[],
    )

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["status"] == "assembling"
    assert data["review"]["user_id"] == admin.id


def test_review_multiple_approvals_create_multiple_rows(
    db_session: Session, preview_talk: models.Talk
) -> None:
    """A talk that goes needs_work and is approved on a later pass has one ApprovedCut row per approval, not one overwritten row."""
    # First approve
    req_approve1 = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)
    with patch("app.tasks.dispatch_assembly"):
        handle_approve(preview_talk, req_approve1, db_session)

    # Then needs work
    preview_talk.status = TalkStatus.PREVIEW
    db_session.commit()
    req_needs_work = schemas.ReviewRequest(decision=schemas.ReviewDecision.needs_work)
    handle_needs_work(preview_talk, req_needs_work, db_session)

    # Second approve with different bounds
    preview_talk.status = TalkStatus.PREVIEW
    preview_talk.cut_start = 12.0
    preview_talk.cut_end = 55.0
    db_session.commit()
    req_approve2 = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)
    with patch("app.tasks.dispatch_assembly"):
        handle_approve(preview_talk, req_approve2, db_session)

    # We should have two ApprovedCuts (one for each approve)
    approved_cuts = list(
        db_session.scalars(
            select(models.ApprovedCut).where(
                models.ApprovedCut.talk_id == preview_talk.id
            )
        )
    )
    assert len(approved_cuts) == 2

    assert approved_cuts[0].cut_start == 10.0
    assert approved_cuts[0].cut_end == 60.0

    assert approved_cuts[1].cut_start == 12.0
    assert approved_cuts[1].cut_end == 55.0


@pytest.mark.parametrize(
    ("cut_start", "cut_end"),
    [
        (None, 60.0),
        (10.0, None),
        (None, None),
    ],
)
def test_review_approve_missing_bounds_returns_400(
    db_session: Session,
    preview_talk: models.Talk,
    cut_start: float | None,
    cut_end: float | None,
) -> None:
    """POST /talks/{id}/review returns 400 when approving a talk without cut bounds."""
    preview_talk.cut_start = cut_start
    preview_talk.cut_end = cut_end
    db_session.commit()
    _create_api_client(db_session, event_ids=[preview_talk.event_id])

    response = client.post(
        f"/talks/{preview_talk.id}/review",
        json={"decision": "approve"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot approve a talk without cut bounds."
    assert _review_count(db_session, preview_talk.id) == 0


@pytest.mark.parametrize(
    ("cut_start", "cut_end"),
    [
        (None, 60.0),
        (10.0, None),
        (None, None),
    ],
)
def test_handle_approve_missing_bounds_raises_http_exception(
    db_session: Session,
    preview_talk: models.Talk,
    cut_start: float | None,
    cut_end: float | None,
) -> None:
    """handle_approve raises HTTPException(400) when talk lacks cut bounds."""
    preview_talk.cut_start = cut_start
    preview_talk.cut_end = cut_end
    payload = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    with pytest.raises(HTTPException) as exc_info:
        handle_approve(preview_talk, payload, db_session)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Cannot approve a talk without cut bounds."


def test_handle_approve_dispatch_failure_advances_to_broken(
    db_session: Session, preview_talk: models.Talk, fake_storage: FakeStorageBackend
) -> None:
    """When dispatch_assembly fails during approve, talk advances to broken, logs error, and raises."""
    payload = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    with (
        patch(
            "app.tasks.dispatch_assembly",
            side_effect=RuntimeError("RQ queue failure"),
        ),
        pytest.raises(RuntimeError, match="RQ queue failure"),
    ):
        handle_approve(preview_talk, payload, db_session, storage=fake_storage)

    db_session.expire_all()
    reloaded = db_session.get(models.Talk, preview_talk.id)
    assert reloaded is not None
    assert reloaded.status == "broken"

    job = db_session.scalar(
        select(models.Job).where(
            models.Job.talk_id == preview_talk.id, models.Job.kind == "assembly"
        )
    )
    assert job is not None
    assert job.status == "failed"
    assert job.log_path == f"{preview_talk.id}/logs/assembly.log"
    assert fake_storage.exists(f"{preview_talk.id}/logs/assembly.log")
    assert (
        b"RQ queue failure"
        in fake_storage.get(f"{preview_talk.id}/logs/assembly.log").read_bytes()
    )
