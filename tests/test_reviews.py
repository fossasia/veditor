"""Tests for speaker review endpoint (POST /talks/{talk_id}/review) and review handlers."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models, schemas
from app.auth import get_client
from app.db import get_db
from app.main import app
from app.review_handlers import (
    DECISION_HANDLERS,
    handle_approve,
    handle_needs_work,
    handle_reject,
)

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
    """POST /talks/{id}/review without API key returns 401."""
    response = client.post(
        "/talks/1/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing API Key"


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
        ("needs_work", None, "needs_work"),
        ("needs_work", "Audio is cut off at the start", "needs_work"),
        ("reject", None, "pending_bounds"),
        ("reject", "Not suitable for publication", "pending_bounds"),
    ],
)
def test_review_valid_decisions_success(
    mock_db, preview_talk, decision, note, expected_status
):
    """POST /talks/{id}/review returns 200 with ReviewResponse and atomic Review audit trail."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.return_value = preview_talk

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

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
    assert resp_work.talk.status == "needs_work"
    assert resp_work.review is not None
    assert resp_work.review.decision == "needs_work"
    assert resp_work.review.note == "Fix cut"

    # Reset talk status for reject test -> pending_bounds
    preview_talk.status = "preview"
    req_reject = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.reject, note="Reset bounds"
    )
    resp_reject = handle_reject(preview_talk, req_reject, mock_db)
    assert resp_reject.talk.id == preview_talk.id
    assert resp_reject.talk.status == "pending_bounds"
    assert resp_reject.review is not None
    assert resp_reject.review.decision == "reject"
    assert resp_reject.review.note == "Reset bounds"


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
    try:
        probe_db = SessionLocal()
        probe_db.execute(select(1))
    except SQLAlchemyError, OSError:
        pytest.skip("Database connection unavailable for concurrent integration test")
    finally:
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
        assert final_talk.status in ("pending_intro_outro", "needs_work")
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
