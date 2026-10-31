from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.db import Base, engine
from app.main import app
from app.pipeline.detect import match_talks_to_recording
from app.security import create_session_token


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def temp_storage(tmp_path):
    from app.storage import LocalDiskBackend, get_storage_backend

    storage = LocalDiskBackend(tmp_path / "test_storage")
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


class DummyTalk:
    def __init__(self, id: int, start: datetime, end: datetime):
        self.id = id
        self.start = start
        self.end = end


def test_match_talks_to_recording_pre_event_trimming():
    """If recording starts at 08:30 and talk starts at 09:00, cut_start_offset is 1800s."""
    rec_start = datetime(2026, 10, 30, 8, 30, tzinfo=UTC)
    rec_duration = 5 * 3600  # 5 hours -> until 13:30

    talk1 = DummyTalk(
        1,
        datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 30, 9, 45, tzinfo=UTC),
    )
    matches = match_talks_to_recording([talk1], rec_start, rec_duration)
    assert len(matches) == 1
    m = matches[0]
    assert m.talk_id == 1
    # 09:00 - 08:30 = 30 minutes = 1800 seconds auto-trimmed
    assert m.cut_start_offset == 1800.0
    # 09:45 - 08:30 = 75 minutes = 4500 seconds
    assert m.cut_end_offset == 4500.0
    assert m.is_full is True
    assert m.overlap_fraction == 1.0


def test_match_talks_to_recording_filters_outside_sessions():
    """Sessions outside the video window (e.g. afternoon when morning video is uploaded) are excluded."""
    rec_start = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    rec_duration = 5 * 3600  # 9:00 AM to 2:00 PM (14:00)

    # Morning talk (10:00 - 11:00) -> fully covered
    morning_talk = DummyTalk(
        1,
        datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        datetime(2026, 10, 30, 11, 0, tzinfo=UTC),
    )
    # Afternoon talk (14:30 - 15:30) -> outside window
    afternoon_talk = DummyTalk(
        2,
        datetime(2026, 10, 30, 14, 30, tzinfo=UTC),
        datetime(2026, 10, 30, 15, 30, tzinfo=UTC),
    )

    matches = match_talks_to_recording(
        [morning_talk, afternoon_talk], rec_start, rec_duration
    )
    assert len(matches) == 1
    assert matches[0].talk_id == 1


def test_match_talks_to_recording_partial_overlap():
    """Sessions that cross the end boundary have partial overlap calculated."""
    rec_start = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    rec_duration = 2 * 3600  # 9:00 to 11:00

    # Talk from 10:30 to 11:30 (1 hour total, 30 min in window)
    talk = DummyTalk(
        1,
        datetime(2026, 10, 30, 10, 30, tzinfo=UTC),
        datetime(2026, 10, 30, 11, 30, tzinfo=UTC),
    )
    matches = match_talks_to_recording(
        [talk], rec_start, rec_duration, min_overlap_fraction=0.3
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.overlap_fraction == 0.5
    assert m.is_full is False
    assert m.cut_start_offset == 5400.0  # 1.5h into recording
    assert m.cut_end_offset == 7200.0  # 2.0h (end of recording)


def test_recording_studio_routes_flow(client: TestClient, db_session):
    """Test full flow: view studio page, stream video, bulk approve with handoff, reject."""
    # 1. Create organizer user and event
    user = models.User(
        email="rec_organizer@example.com",
        hashed_password="fake",
        role="organizer",
    )
    db_session.add(user)
    db_session.flush()

    event = models.Event(
        name="PyCon 2026",
        created_by_user_id=user.id,
    )
    db_session.add(event)
    db_session.flush()

    # 2. Create talks in room "Main Hall"
    t1 = models.Talk(
        event_id=event.id,
        room="Main Hall",
        title="Opening Keynote",
        start=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        speaker_email="speaker1@example.com",
        status="waiting_for_files",
    )
    t2 = models.Talk(
        event_id=event.id,
        room="Main Hall",
        title="Deep Dive PyAV",
        start=datetime(2026, 10, 30, 10, 15, tzinfo=UTC),
        end=datetime(2026, 10, 30, 11, 15, tzinfo=UTC),
        speaker_email="speaker2@example.com",
        status="waiting_for_files",
    )
    db_session.add_all([t1, t2])
    db_session.flush()

    # 3. Create a RoomRecording
    rec = models.RoomRecording(
        event_id=event.id,
        room="Main Hall",
        recording_start=datetime(2026, 10, 30, 8, 30, tzinfo=UTC),
        duration_seconds=3.0 * 3600,  # 8:30 to 11:30
        status="ready",
        created_by_user_id=user.id,
    )
    db_session.add(rec)
    db_session.commit()

    # 4. Unauthenticated access redirects to login
    unauth_resp = client.get(f"/studio/recordings/{rec.id}", follow_redirects=False)
    assert unauth_resp.status_code == 302
    assert f"/login?next=/studio/recordings/{rec.id}" in unauth_resp.headers["location"]

    # 5. Authenticated access renders HTML
    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)

    page_resp = client.get(f"/studio/recordings/{rec.id}")
    assert page_resp.status_code == 200
    assert "Recording Studio" in page_resp.text
    assert "Main Hall" in page_resp.text
    assert "Opening Keynote" in page_resp.text
    assert "Deep Dive PyAV" in page_resp.text

    # 6. Bulk approve both talks in one go
    with patch("app.queue.light_queue.enqueue") as mock_enqueue:
        approve_resp = client.post(
            f"/studio/recordings/{rec.id}/bulk-approve",
            json={
                "talk_ids": [t1.id, t2.id],
                "include_intro": True,
                "include_outro": True,
                "intro_source": "generated",
                "outro_source": "generated",
                "handoff": True,
            },
        )
        assert approve_resp.status_code == 200
        data = approve_resp.json()
        assert data["status"] == "ok"
        assert data["approved_count"] == 2

        # Check DB state
        db_session.refresh(t1)
        db_session.refresh(t2)
        db_session.refresh(rec)

        assert rec.status == "attached"
        assert t1.room_recording_id == rec.id
        assert t2.room_recording_id == rec.id

        # Talk 1 (09:00 - 10:00) with 08:30 start:
        # cut_start: (09:00 - 08:30) = 1800s
        # cut_end: (10:00 - 08:30) = 5400s
        assert t1.cut_start == 1800.0
        assert t1.cut_end == 5400.0
        assert t1.include_intro is True
        assert t1.include_outro is True
        assert t1.status == "pending_bounds"
        assert t1.handed_off_at is not None

        # Talk 2 (10:15 - 11:15) with 08:30 start:
        # cut_start: (10:15 - 08:30) = 6300s
        # cut_end: (11:15 - 08:30) = 9900s
        assert t2.cut_start == 6300.0
        assert t2.cut_end == 9900.0
        assert t2.status == "pending_bounds"
        assert t2.handed_off_at is not None

        # Both speakers should have had handoff email queued
        assert mock_enqueue.call_count == 2

    # 7. Reject endpoint
    reject_resp = client.post(f"/studio/recordings/{rec.id}/reject")
    assert reject_resp.status_code == 200
    db_session.refresh(rec)
    assert rec.status == "rejected"


def test_stage_only_and_bulk_approval_attaches_only_selected_talks(
    client: TestClient, db_session, temp_storage, tmp_path
):
    """When stage_only is used, talks are not modified until bulk approval is clicked."""
    user = models.User(
        email="stage_org@example.com",
        hashed_password="fake",
        role="organizer",
    )
    db_session.add(user)
    db_session.flush()

    event = models.Event(
        name="Stage Only Event",
        created_by_user_id=user.id,
    )
    db_session.add(event)
    db_session.flush()

    # Create 3 talks in room
    t1 = models.Talk(
        event_id=event.id,
        room="Room 101",
        title="Session 1",
        start=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 9, 30, tzinfo=UTC),
        speaker_email="s1@example.com",
        status="waiting_for_files",
    )
    t2 = models.Talk(
        event_id=event.id,
        room="Room 101",
        title="Session 2",
        start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 10, 30, tzinfo=UTC),
        speaker_email="s2@example.com",
        status="waiting_for_files",
    )
    t3 = models.Talk(
        event_id=event.id,
        room="Room 101",
        title="Afternoon Session 3",
        start=datetime(2026, 10, 30, 14, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 15, 0, tzinfo=UTC),
        speaker_email="s3@example.com",
        status="waiting_for_files",
    )
    db_session.add_all([t1, t2, t3])
    db_session.commit()

    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)

    # 1. Attach room recording with stage_only=True
    from tests.conftest import generate_clip

    clip = generate_clip(0.5, output_dir=tmp_path)
    try:
        with open(clip, "rb") as f_vid:
            attach_res = client.post(
                "/talks/room/attach-recording",
                data={
                    "room": "Room 101",
                    "event_id": str(event.id),
                    "stage_only": "true",
                    "recording_start": "2026-10-30T08:30:00Z",
                },
                files={"file": ("morning_raw.mp4", f_vid, "video/mp4")},
            )
            assert attach_res.status_code == 200
            data = attach_res.json()
            assert data["attached_count"] == 0
            rec_id = data["recording_id"]
            assert rec_id is not None

        # Verify talks were NOT touched
        db_session.refresh(t1)
        db_session.refresh(t2)
        db_session.refresh(t3)
        assert t1.status == "waiting_for_files"
        assert t2.status == "waiting_for_files"
        assert t3.status == "waiting_for_files"
        assert not temp_storage.exists(f"{t1.id}/raw/raw.mp4")

        # 2. Upload custom bumper for the recording
        bumper_clip = generate_clip(0.2, output_dir=tmp_path)
        with open(bumper_clip, "rb") as f_b:
            b_res = client.post(
                f"/studio/recordings/{rec_id}/bumpers/upload",
                data={"kind": "intro"},
                files={"file": ("intro.mp4", f_b, "video/mp4")},
            )
            assert b_res.status_code == 200
            custom_intro_path = b_res.json()["path"]
            assert custom_intro_path

        # 3. Bulk approve ONLY t1 and t2 (morning sessions), leaving t3 unattached
        with patch("app.queue.light_queue.enqueue"):
            approve_res = client.post(
                f"/studio/recordings/{rec_id}/bulk-approve",
                json={
                    "talk_ids": [t1.id, t2.id],
                    "include_intro": True,
                    "include_outro": False,
                    "intro_source": "custom",
                    "custom_intro_path": custom_intro_path,
                    "handoff": True,
                },
            )
            assert approve_res.status_code == 200
            assert approve_res.json()["approved_count"] == 2

        db_session.refresh(t1)
        db_session.refresh(t2)
        db_session.refresh(t3)

        # t1 and t2 are now attached and advanced
        assert t1.status == "pending_bounds"
        assert t1.custom_intro_path == custom_intro_path
        assert t1.include_intro is True
        assert t2.status == "pending_bounds"
        assert temp_storage.exists(f"{t1.id}/raw/raw.mp4")
        assert temp_storage.exists(f"{t2.id}/raw/raw.mp4")

        # t3 was NOT selected and remains waiting_for_files (ready for afternoon recording)
        assert t3.status == "waiting_for_files"
        assert not temp_storage.exists(f"{t3.id}/raw/raw.mp4")
        assert t3.room_recording_id is None
    finally:
        clip.unlink(missing_ok=True)
