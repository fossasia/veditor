"""Tests for speaker review endpoint (POST /talks/{talk_id}/review) and review handlers."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from unittest.mock import call as mock_call

import pytest
from fastapi.testclient import TestClient

from app import models, schemas
from app.auth import CurrentUser, get_client, get_current_user
from app.db import get_db
from app.main import app
from app.review_handlers import (
    DECISION_HANDLERS,
    handle_approve,
    handle_needs_work,
    handle_reject,
)
from app.storage import get_storage_backend

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    """Ensure dependency overrides are cleared after each test."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_db():
    db = MagicMock()

    def fake_flush():
        for call in db.add.call_args_list:
            obj = call[0][0]
            if getattr(obj, "id", None) is None:
                obj.id = 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)

    db.flush.side_effect = fake_flush

    def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)

    db.refresh.side_effect = fake_refresh

    # Allow chaining of .filter(...).with_for_update().first() to resolve to filter's first
    mock_filter = db.query.return_value.filter.return_value
    mock_filter.with_for_update.return_value = mock_filter

    return db


@pytest.fixture
def preview_talk():
    return models.Talk(
        id=1,
        event_id=1,
        title="Test Review Talk",
        room="Room 101",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
        cut_start=10.0,
        cut_end=60.0,
        raw_duration_seconds=120.0,
    )


def test_review_unauthorized():
    """POST /talks/{id}/review without credentials returns 401."""
    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_review_invalid_decision_returns_422_without_db_query(mock_db):
    """POST with invalid decision fails schema validation with 422 before querying DB."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "invalid_decision"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422
    # Verify no database queries were executed
    mock_db.query.assert_not_called()


def test_review_talk_not_found(mock_db):
    """POST /talks/{id}/review returns 404 if talk does not exist."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = None

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/999/review",
        json={"decision": "approve"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_review_forbidden_event(mock_db, preview_talk):
    """POST /talks/{id}/review returns 403 if client is not authorized for talk's event."""
    mock_client = models.Client(id=1, event_ids=[2])  # talk.event_id is 1
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
        headers={"X-API-Key": "valid_key"},
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
def test_review_conflict_non_preview_state(mock_db, preview_talk, invalid_status):
    """POST /talks/{id}/review returns 409 if talk status is not 'preview'."""
    preview_talk.status = invalid_status
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Cannot review talk in status '{invalid_status}'; talk must be in 'preview'"
    )


@pytest.mark.parametrize(
    ("decision", "note", "expected_status"),
    [
        ("approve", None, "pending_intro_outro"),
        ("approve", "Looks great!", "pending_intro_outro"),
        ("needs_work", None, "pending_bounds"),
        ("needs_work", "Audio is cut off at the start", "pending_bounds"),
        ("reject", None, "rejected"),
        ("reject", "Not suitable for publication", "rejected"),
    ],
)
def test_review_valid_decisions_success(
    mock_db, preview_talk, fake_storage, decision, note, expected_status
):
    """POST /talks/{id}/review returns 200 with ReviewResponse and atomic Review audit trail."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    body = {"decision": decision}
    if note is not None:
        body["note"] = note

    response = client.post(
        "/talks/1/review",
        json=body,
        headers={"X-API-Key": "valid_key"},
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
    mock_db.add.assert_called_once()
    mock_db.flush.assert_called_once()
    mock_db.commit.assert_called_once()


@pytest.mark.parametrize(
    ("decision", "handler_name"),
    [
        (schemas.ReviewDecision.approve, "handle_approve"),
        (schemas.ReviewDecision.needs_work, "handle_needs_work"),
        (schemas.ReviewDecision.reject, "handle_reject"),
    ],
)
def test_review_dispatch_invokes_correct_handler(
    mock_db, preview_talk, decision, handler_name
):
    """Verify that route dispatches to the registered handler in DECISION_HANDLERS."""
    assert DECISION_HANDLERS[decision].__name__ == handler_name
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

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
            "/talks/1/review",
            json={"decision": decision.value, "note": "Dispatch test note"},
            headers={"X-API-Key": "valid_key"},
        )
        assert response.status_code == 200
        mock_handler.assert_called_once()
        called_talk, called_payload, called_db = mock_handler.call_args[0]
        assert called_talk == preview_talk
        assert called_payload.decision == decision
        assert called_payload.note == "Dispatch test note"
        assert called_db == mock_db


def test_handlers_direct_persistence_and_advance(preview_talk, mock_db):
    """Directly test handle_approve, handle_needs_work, handle_reject with DB transaction."""
    # Test handle_approve -> pending_intro_outro
    req_approve = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.approve, note="Approve note"
    )
    resp_approve = handle_approve(preview_talk, req_approve, mock_db)
    assert resp_approve.talk.id == preview_talk.id
    assert resp_approve.talk.status == "pending_intro_outro"
    assert resp_approve.review is not None
    assert resp_approve.review.decision == "approve"
    assert resp_approve.review.note == "Approve note"

    # Reset talk status for needs_work test
    preview_talk.status = "preview"
    req_work = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.needs_work, note="Fix cut"
    )
    resp_work = handle_needs_work(preview_talk, req_work, mock_db)
    assert resp_work.talk.id == preview_talk.id
    assert resp_work.talk.status == "pending_bounds"
    assert resp_work.review is not None
    assert resp_work.review.decision == "needs_work"
    assert resp_work.review.note == "Fix cut"

    # Reset talk status for reject test -> rejected
    preview_talk.status = "preview"
    req_reject = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.reject, note="Reset bounds"
    )
    resp_reject = handle_reject(preview_talk, req_reject, mock_db)
    assert resp_reject.talk.id == preview_talk.id
    assert resp_reject.talk.status == "rejected"
    assert resp_reject.talk.cut_start is None
    assert resp_reject.talk.cut_end is None
    assert preview_talk.cut_start is None
    assert preview_talk.cut_end is None
    assert preview_talk.status == "rejected"
    assert resp_reject.review is not None
    assert resp_reject.review.decision == "reject"
    assert resp_reject.review.note == "Reset bounds"


def test_handle_reject_clears_cut_bounds_reset_to_raw(mock_db, fake_storage):
    """Rejecting a talk in preview clears cut bounds, transitions to rejected, purges cut/preview files, and enqueues no jobs."""
    talk = models.Talk(
        id=42,
        event_id=1,
        title="Reject Reset Talk",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
        cut_start=25.5,
        cut_end=85.0,
        raw_duration_seconds=120.0,
    )
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = talk

    fake_storage.put("42/raw/video.mp4", b"raw footage")
    fake_storage.put("42/cut/cut.mp4", b"cut footage")
    fake_storage.put("42/preview/preview.mp4", b"preview video")

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch("app.queue.light_queue.enqueue") as mock_light_enqueue,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        response = client.post(
            "/talks/42/review",
            json={"decision": "reject", "note": "Bounds inaccurate, reset to raw"},
            headers={"X-API-Key": "valid_key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["talk"]["status"] == "rejected"
        assert data["talk"]["cut_start"] is None
        assert data["talk"]["cut_end"] is None
        assert data["talk"]["raw_duration_seconds"] == 120.0
        assert data["review"]["decision"] == "reject"
        assert data["review"]["note"] == "Bounds inaccurate, reset to raw"
        assert talk.cut_start is None
        assert talk.cut_end is None
        assert talk.status == "rejected"

        assert not fake_storage.exists("42/cut/cut.mp4")
        assert not fake_storage.exists("42/preview/preview.mp4")
        assert fake_storage.exists("42/raw/video.mp4")

        mock_light_enqueue.assert_not_called()
        mock_heavy_enqueue.assert_not_called()


def test_handle_reject_storage_delete_error_resilient(mock_db):
    """Storage deletion errors in handle_reject do not crash endpoint (returns 200) and both targets are attempted."""
    talk = models.Talk(
        id=42,
        event_id=1,
        title="Reject Error Resilience Talk",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
        cut_start=25.5,
        cut_end=85.0,
        raw_duration_seconds=120.0,
    )
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = talk

    mock_storage = MagicMock()
    mock_storage.delete.side_effect = [
        RuntimeError("Storage connection failed"),
        None,
        None,
        None,
        None,
    ]

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    response = client.post(
        "/talks/42/review",
        json={"decision": "reject", "note": "Failed cut deletion shouldn't 500"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["status"] == "rejected"
    assert data["talk"]["cut_start"] is None
    assert data["talk"]["cut_end"] is None
    assert data["review"]["decision"] == "reject"
    assert talk.status == "rejected"
    assert talk.cut_start is None
    assert talk.cut_end is None

    assert mock_storage.delete.call_count == 5
    assert mock_storage.delete.call_args_list == [
        mock_call("42/cut"),
        mock_call("42/preview"),
        mock_call("42/assemble"),
        mock_call("42/intro"),
        mock_call("42/outro"),
    ]


def test_simulated_commit_failure_rolls_back_review_insert(preview_talk, mock_db):
    """Simulated failure of the DB commit rolls back the Review insert and talk transition."""
    mock_db.commit.side_effect = RuntimeError("Simulated DB write failure")
    req = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    with pytest.raises(RuntimeError, match="Simulated DB write failure"):
        handle_approve(preview_talk, req, mock_db)

    mock_db.rollback.assert_called_once()


def test_simulated_flush_failure_rolls_back_review_insert(preview_talk, mock_db):
    """Simulated failure of the DB flush rolls back the Review insert and talk transition."""
    mock_db.flush.side_effect = RuntimeError("Simulated DB flush failure")
    req = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    with pytest.raises(RuntimeError, match="Simulated DB flush failure"):
        handle_approve(preview_talk, req, mock_db)

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()


def test_review_locks_talk_row_with_for_update(mock_db, preview_talk):
    """POST /talks/{id}/review acquires row-level lock via with_for_update() on talk query."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    mock_db.query.return_value.filter.return_value.with_for_update.assert_called_once()


def test_flush_before_commit_and_no_refresh(mock_db, preview_talk):
    """Review handler flushes before commit and makes zero db.refresh calls."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    mock_db.flush.assert_called_once()
    mock_db.commit.assert_called_once()
    mock_db.refresh.assert_not_called()


def test_simulated_advance_failure_rolls_back_review_insert(preview_talk, mock_db):
    """Simulated failure of the state transition rolls back the Review insert."""
    preview_talk.status = (
        "waiting_for_files"  # Invalid current state for advance to transcoding
    )
    req = schemas.ReviewRequest(decision=schemas.ReviewDecision.approve)

    from app.states import InvalidTransitionError

    with pytest.raises(InvalidTransitionError):
        handle_approve(preview_talk, req, mock_db)

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()


def test_append_only_audit_trail_relationship():
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


def test_concurrent_reviews_atomic_transition_and_single_review():
    """Verify concurrent review submissions result in exactly one success and one 409."""
    import concurrent.futures

    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from app.auth import hash_api_key
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
    )
    db.add(talk)
    db.commit()
    talk_id = talk.id
    db.close()

    try:

        def post_review(decision: str):
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
        reviews = db.query(models.Review).filter(models.Review.talk_id == talk_id).all()
        assert len(reviews) == 1

        final_talk = db.query(models.Talk).filter(models.Talk.id == talk_id).one()
        assert final_talk.status in ("pending_intro_outro", "pending_bounds")
        assert final_talk.status != "preview"
        assert reviews[0].decision in ("approve", "needs_work")
        db.close()
    finally:
        clean_db = SessionLocal()
        clean_db.query(models.Review).filter(models.Review.talk_id == talk_id).delete()
        clean_db.query(models.Talk).filter(models.Talk.id == talk_id).delete()
        clean_db.query(models.Client).filter(models.Client.id == client_id).delete()
        clean_db.query(models.Event).filter(models.Event.id == event_id).delete()
        clean_db.commit()
        clean_db.close()


def test_approve_blocks_at_pending_intro_outro_without_enqueuing_jobs(
    mock_db, preview_talk
):
    """
    Acceptance Criteria:
    - POST /talks/{id}/review with approve transitions talk to 'pending_intro_outro'.
    - No RQ job is enqueued as a direct result of this transition.
    - No Job model record is created in the database.
    - The talk remains parked in 'pending_intro_outro' without auto-advancing.
    """
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    with (
        patch("app.queue.light_queue.enqueue") as mock_light_enqueue,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        response = client.post(
            f"/talks/{preview_talk.id}/review",
            json={"decision": "approve", "note": "Approved by speaker"},
            headers={"X-API-Key": "valid_key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["talk"]["status"] == "pending_intro_outro"
        assert preview_talk.status == "pending_intro_outro"

        # Explicit non-enqueue on this path
        mock_light_enqueue.assert_not_called()
        mock_heavy_enqueue.assert_not_called()

        # Verify only Review was added to db, no Job model was created
        added_types = [type(call[0][0]) for call in mock_db.add.call_args_list]
        assert models.Review in added_types
        assert models.Job not in added_types

        # Talk remains parked in pending_intro_outro without auto-progression
        assert preview_talk.status == "pending_intro_outro"


def test_review_needs_work_transitions_to_pending_bounds_retains_offsets_and_no_job_enqueued(
    mock_db, preview_talk
):
    """POST /talks/{id}/review with needs_work transitions talk to pending_bounds,

    preserves existing cut_start and cut_end offsets intact, and enqueues zero jobs.
    """
    preview_talk.cut_start = 12.5
    preview_talk.cut_end = 75.0
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    with (
        patch("app.queue.light_queue.enqueue") as mock_light_enqueue,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        response = client.post(
            "/talks/1/review",
            json={"decision": "needs_work", "note": "Audio cut off at beginning"},
            headers={"X-API-Key": "valid_key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["talk"]["status"] == "pending_bounds"
        assert data["talk"]["cut_start"] == 12.5
        assert data["talk"]["cut_end"] == 75.0
        assert preview_talk.status == "pending_bounds"
        assert preview_talk.cut_start == 12.5
        assert preview_talk.cut_end == 75.0
        assert data["review"]["decision"] == "needs_work"
        assert data["review"]["note"] == "Audio cut off at beginning"

        # Explicit invariant: no RQ jobs enqueued on needs_work transition
        mock_light_enqueue.assert_not_called()
        mock_heavy_enqueue.assert_not_called()


def test_needs_work_subsequent_cut_overwrites_outputs():
    """Verify that after needs_work transitions to pending_bounds with bounds intact:

    1. Prior cut and preview artifacts remain in place.
    2. Raw recording remains completely untouched.
    3. Subsequent POST /cut with adjusted bounds transitions to cutting and enqueues cut.
    4. Subsequent cut and preview runs overwrite previous outputs.
    """
    from app.storage import get_storage_backend
    from tests.conftest import FakeStorageBackend

    fake_storage = FakeStorageBackend()
    fake_storage.put("1/raw/video.mp4", b"original raw video bytes")
    fake_storage.put("1/cut/cut.mp4", b"old cut content v1")
    fake_storage.put("1/preview/preview.mp4", b"old preview content v1")

    mock_client = models.Client(id=1, event_ids=[1])
    talk = models.Talk(
        id=1,
        event_id=1,
        title="Needs Work E2E Talk",
        room="Room 101",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="preview",
        cut_start=10.0,
        cut_end=60.0,
        raw_duration_seconds=120.0,
    )

    mock_db = MagicMock()
    mock_filter = mock_db.query.return_value.filter.return_value
    mock_filter.with_for_update.return_value = mock_filter
    mock_filter.first.return_value = talk

    def fake_flush():
        for call in mock_db.add.call_args_list:
            obj = call[0][0]
            if getattr(obj, "id", None) is None:
                obj.id = 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)

    mock_db.flush.side_effect = fake_flush

    def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)

    mock_db.refresh.side_effect = fake_refresh

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    # Step 1: Review decision 'needs_work'
    with (
        patch("app.queue.light_queue.enqueue") as mock_light_q,
        patch("app.queue.heavy_queue.enqueue") as mock_heavy_q,
    ):
        resp_review = client.post(
            "/talks/1/review",
            json={"decision": "needs_work", "note": "Adjust start boundary"},
            headers={"X-API-Key": "valid_key"},
        )
        assert resp_review.status_code == 200
        review_data = resp_review.json()
        assert review_data["talk"]["status"] == "pending_bounds"
        assert review_data["talk"]["cut_start"] == 10.0
        assert review_data["talk"]["cut_end"] == 60.0
        assert talk.status == "pending_bounds"
        assert talk.cut_start == 10.0
        assert talk.cut_end == 60.0

        # No background jobs enqueued
        mock_light_q.assert_not_called()
        mock_heavy_q.assert_not_called()

        # Prior artifacts still exist, raw untouched
        assert fake_storage.get("1/raw/video.mp4").read_bytes() == (
            b"original raw video bytes"
        )
        assert fake_storage.get("1/cut/cut.mp4").read_bytes() == b"old cut content v1"
        assert fake_storage.get("1/preview/preview.mp4").read_bytes() == (
            b"old preview content v1"
        )

    # Step 2: Speaker resubmits adjusted bounds via POST /talks/{id}/cut
    with patch("app.routes.talks.light_queue.enqueue") as mock_cut_enqueue:
        resp_cut = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:00:20", "cut_end": "00:00:50"},
            headers={"X-API-Key": "valid_key"},
        )
        assert resp_cut.status_code == 202
        cut_data = resp_cut.json()
        assert cut_data["status"] == "cutting"
        assert cut_data["cut_start"] == 20.0
        assert cut_data["cut_end"] == 50.0
        assert talk.status == "cutting"
        assert talk.cut_start == 20.0
        assert talk.cut_end == 50.0

        # Verify job_cut was enqueued
        mock_cut_enqueue.assert_called_once()
        call_args = mock_cut_enqueue.call_args
        queued_cut = call_args[0][0]
        cut_args = call_args[0][1:]
        assert call_args[0][1] == 1  # talk_id
        assert call_args[0][2] == "1/raw/video.mp4"  # raw_key

    # Step 3: Run queued cut and preview workers to overwrite artifacts
    mock_db.__enter__.return_value = mock_db
    mock_db.get.side_effect = lambda model, obj_id: (
        talk if model == models.Talk else MagicMock()
    )

    with (
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch(
            "app.tasks.cut",
            side_effect=lambda inp, out, s, e: Path(out).write_bytes(
                b"new cut content v2"
            ),
        ),
        patch(
            "app.tasks.generate_preview",
            side_effect=lambda inp, out, preset: Path(out).write_bytes(
                b"new preview content v2"
            ),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_preview_enqueue,
    ):
        queued_cut(*cut_args)
        assert talk.status == "generating_previews"

        mock_preview_enqueue.assert_called_once()
        queued_preview = mock_preview_enqueue.call_args[0][0]
        preview_args = mock_preview_enqueue.call_args[0][1:]

        queued_preview(*preview_args)
        assert talk.status == "preview"
        assert mock_preview_enqueue.call_count == 1

    assert fake_storage.get("1/cut/cut.mp4").read_bytes() == b"new cut content v2"
    assert fake_storage.get("1/preview/preview.mp4").read_bytes() == (
        b"new preview content v2"
    )
    # Raw recording is still untouched
    assert fake_storage.get("1/raw/video.mp4").read_bytes() == (
        b"original raw video bytes"
    )


def test_review_human_user_role_forbidden(mock_db, preview_talk):
    """POST /talks/{id}/review returns 403 if human caller has role 'user'."""
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=10, email="user@example.com", role="user", source="cookie", event_ids=[]
    )
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 403
    assert "Operation requires minimum role 'organizer'" in response.json()["detail"]


def test_review_human_organizer_unowned_event_forbidden(mock_db, preview_talk):
    """POST /talks/{id}/review returns 403 if organizer does not own the event."""
    unowned_event = models.Event(id=1, name="Other Event", created_by_user_id=999)

    def mock_query(model):
        m = MagicMock()
        if model == models.Event:
            m.filter.return_value.first.return_value = unowned_event
        else:
            m.filter.return_value.first.return_value = preview_talk
            m.filter.return_value.with_for_update.return_value = m.filter.return_value
        return m

    mock_db.query.side_effect = mock_query

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=10,
        email="org@example.com",
        role="organizer",
        source="cookie",
        event_ids=[],
    )
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "User is not authorized to access this event"


def test_review_human_organizer_owned_event_success(
    mock_db, preview_talk, fake_storage
):
    """POST /talks/{id}/review succeeds for organizer who owns the event."""
    owned_event = models.Event(id=1, name="Owned Event", created_by_user_id=10)

    def mock_query(model):
        m = MagicMock()
        if model == models.Event:
            m.filter.return_value.first.return_value = owned_event
        else:
            m.filter.return_value.first.return_value = preview_talk
            m.filter.return_value.with_for_update.return_value = m.filter.return_value
        return m

    mock_db.query.side_effect = mock_query

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=10,
        email="org@example.com",
        role="organizer",
        source="cookie",
        event_ids=[],
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["status"] == "pending_intro_outro"
    assert data["review"]["user_id"] == 10


def test_review_human_admin_success(mock_db, preview_talk, fake_storage):
    """POST /talks/{id}/review succeeds for admin even if event created by someone else."""
    event = models.Event(id=1, name="Event", created_by_user_id=999)

    def mock_query(model):
        m = MagicMock()
        if model == models.Event:
            m.filter.return_value.first.return_value = event
        else:
            m.filter.return_value.first.return_value = preview_talk
            m.filter.return_value.with_for_update.return_value = m.filter.return_value
        return m

    mock_db.query.side_effect = mock_query

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1,
        email="admin@example.com",
        role="admin",
        source="cookie",
        event_ids=[],
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["status"] == "pending_intro_outro"
    assert data["review"]["user_id"] == 1
