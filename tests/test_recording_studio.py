from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.config import settings
from app.consts import AuthRole, RecordingStatus, TalkStatus
from app.main import app
from app.pipeline.detect import match_talks_to_recording
from app.security import create_session_token
from app.storage import LocalDiskBackend, get_storage_backend
from tests.conftest import generate_clip


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def temp_storage(tmp_path: Path) -> Generator[Any]:
    storage = LocalDiskBackend(tmp_path / 'test_storage')
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


def test_match_talks_to_recording_pre_event_trimming() -> None:
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


def test_match_talks_to_recording_filters_outside_sessions() -> None:
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


def test_match_talks_to_recording_partial_overlap() -> None:
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


def test_recording_studio_routes_flow(
    client: TestClient,
    db_session: Any,
    temp_storage: Any,
    tmp_path: Path,
) -> None:
    """Test full flow: view studio page, stream video, bulk approve with handoff, reject."""
    # 1. Create organizer user and event
    user = models.User(
        email='rec_organizer@example.com',
        hashed_password='fake',
        role=AuthRole.ORGANIZER,
    )
    db_session.add(user)
    db_session.flush()

    event = models.Event(
        name='PyCon 2026',
        created_by_user_id=user.id,
    )
    db_session.add(event)
    db_session.flush()

    # 2. Create talks in room "Main Hall"
    t1 = models.Talk(
        event_id=event.id,
        room='Main Hall',
        title='Opening Keynote',
        start=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        speaker_email='speaker1@example.com',
        status=TalkStatus.WAITING_FILES,
    )
    t2 = models.Talk(
        event_id=event.id,
        room='Main Hall',
        title='Deep Dive PyAV',
        start=datetime(2026, 10, 30, 10, 15, tzinfo=UTC),
        end=datetime(2026, 10, 30, 11, 15, tzinfo=UTC),
        speaker_email='speaker2@example.com',
        status=TalkStatus.WAITING_FILES,
    )
    db_session.add_all([t1, t2])
    db_session.flush()

    # 3. Create a RoomRecording
    rec = models.RoomRecording(
        event_id=event.id,
        room='Main Hall',
        recording_start=datetime(2026, 10, 30, 8, 30, tzinfo=UTC),
        duration_seconds=3.0 * 3600,  # 8:30 to 11:30
        status=RecordingStatus.READY,
        created_by_user_id=user.id,
    )
    db_session.add(rec)
    db_session.flush()
    rec.storage_key = f'recordings/{rec.id}/recording.mp4'
    clip = generate_clip(1, output_dir=tmp_path)
    temp_storage.link_or_copy(rec.storage_key, clip)
    db_session.commit()

    # 4. Unauthenticated access redirects to login
    unauth_resp = client.get(f'/studio/recordings/{rec.id}', follow_redirects=False)
    assert unauth_resp.status_code == 302
    assert f'/login?next=/studio/recordings/{rec.id}' in unauth_resp.headers['location']

    # 5. Authenticated access renders HTML
    token = create_session_token(user.id, user.role)
    client.cookies.set('veditor_session', token)

    page_resp = client.get(f'/studio/recordings/{rec.id}')
    assert page_resp.status_code == 200
    assert 'Recording Studio' in page_resp.text
    assert 'Main Hall' in page_resp.text
    assert 'Opening Keynote' in page_resp.text
    assert 'Deep Dive PyAV' in page_resp.text

    # 6. Bulk approve both talks in one go
    with patch('app.queue.light_queue.enqueue') as mock_enqueue:
        approve_resp = client.post(
            f'/studio/recordings/{rec.id}/bulk-approve',
            json={
                'talk_ids': [t1.id, t2.id],
                'include_intro': True,
                'include_outro': True,
                'intro_source': 'generated',
                'outro_source': 'generated',
                'handoff': True,
            },
        )
        assert approve_resp.status_code == 200
        data = approve_resp.json()
        assert data['status'] == 'ok'
        assert data['approved_count'] == 2

        # Check DB state
        db_session.refresh(t1)
        db_session.refresh(t2)
        db_session.refresh(rec)

        assert rec.status == RecordingStatus.ATTACHED
        assert t1.room_recording_id == rec.id
        assert t2.room_recording_id == rec.id

        # Talk 1 (09:00 - 10:00) with 08:30 start:
        # cut_start: (09:00 - 08:30) = 1800s
        # cut_end: (10:00 - 08:30) = 5400s
        assert t1.cut_start == 1800.0
        assert t1.cut_end == 5400.0
        assert t1.include_intro is True
        assert t1.include_outro is True
        assert t1.status == TalkStatus.PENDING_BOUNDS
        assert t1.handed_off_at is not None

        # Talk 2 (10:15 - 11:15) with 08:30 start:
        # cut_start: (10:15 - 08:30) = 6300s
        # cut_end: (11:15 - 08:30) = 9900s
        assert t2.cut_start == 6300.0
        assert t2.cut_end == 9900.0
        assert t2.status == TalkStatus.PENDING_BOUNDS
        assert t2.handed_off_at is not None

        # Both speakers should have had handoff email queued
        assert mock_enqueue.call_count == 2

    # 7. Reject endpoint
    reject_resp = client.post(f'/studio/recordings/{rec.id}/reject')
    assert reject_resp.status_code == 409
    db_session.refresh(rec)
    assert rec.status == RecordingStatus.ATTACHED


def test_stage_only_and_bulk_approval_attaches_only_selected_talks(
    client: TestClient,
    db_session: Any,
    temp_storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stage_only is used, talks are not modified until bulk approval is clicked."""
    monkeypatch.setattr(settings, 'ingest_roots', [])
    user = models.User(
        email='stage_org@example.com',
        hashed_password='fake',
        role=AuthRole.ORGANIZER,
    )
    db_session.add(user)
    db_session.flush()

    event = models.Event(
        name='Stage Only Event',
        created_by_user_id=user.id,
    )
    db_session.add(event)
    db_session.flush()

    # Create 3 talks in room
    t1 = models.Talk(
        event_id=event.id,
        room='Room 101',
        title='Session 1',
        start=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 9, 30, tzinfo=UTC),
        speaker_email='s1@example.com',
        status=TalkStatus.WAITING_FILES,
    )
    t2 = models.Talk(
        event_id=event.id,
        room='Room 101',
        title='Session 2',
        start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 10, 30, tzinfo=UTC),
        speaker_email='s2@example.com',
        status=TalkStatus.WAITING_FILES,
    )
    t3 = models.Talk(
        event_id=event.id,
        room='Room 101',
        title='Afternoon Session 3',
        start=datetime(2026, 10, 30, 14, 0, tzinfo=UTC),
        end=datetime(2026, 10, 30, 15, 0, tzinfo=UTC),
        speaker_email='s3@example.com',
        status=TalkStatus.WAITING_FILES,
    )
    db_session.add_all([t1, t2, t3])
    db_session.commit()

    token = create_session_token(user.id, user.role)
    client.cookies.set('veditor_session', token)

    # 1. Attach room recording with stage_only=True
    clip = generate_clip(1, output_dir=tmp_path)
    try:
        with open(clip, 'rb') as f_vid:
            attach_res = client.post(
                '/talks/room/attach-recording',
                data={
                    'room': 'Room 101',
                    'event_id': str(event.id),
                    'stage_only': 'true',
                    'recording_start': '2026-10-30T09:00:00Z',
                },
                files={'file': ('morning_raw.mp4', f_vid, 'video/mp4')},
            )
            assert attach_res.status_code == 200
            data = attach_res.json()
            assert data['attached_count'] == 0
            rec_id = data['recording_id']
            assert rec_id is not None
            staged_rec = db_session.get(models.RoomRecording, rec_id)
            assert staged_rec is not None
            staged_rec.duration_seconds = 10800
            db_session.commit()

        # Verify talks were NOT touched
        db_session.refresh(t1)
        db_session.refresh(t2)
        db_session.refresh(t3)
        assert t1.status == TalkStatus.WAITING_FILES
        assert t2.status == TalkStatus.WAITING_FILES
        assert t3.status == TalkStatus.WAITING_FILES
        assert not temp_storage.exists(f'{t1.id}/raw/raw.mp4')

        # 2. Upload custom bumper for the recording (works even with empty ingest_roots)
        bumper_clip = generate_clip(0.2, output_dir=tmp_path)
        with open(bumper_clip, 'rb') as f_b:
            b_res = client.post(
                f'/studio/recordings/{rec_id}/bumpers/upload',
                data={'kind': 'intro'},
                files={'file': ('intro.mp4', f_b, 'video/mp4')},
            )
            assert b_res.status_code == 200
            custom_intro_path = b_res.json()['path']
            assert custom_intro_path.startswith(f'bumpers/rec_bumper_{rec_id}_intro_')

        # 3. Bulk approve ONLY t1 and t2 (morning sessions), leaving t3 unattached
        with patch('app.queue.light_queue.enqueue'):
            approve_res = client.post(
                f'/studio/recordings/{rec_id}/bulk-approve',
                json={
                    'talk_ids': [t1.id, t2.id],
                    'include_intro': True,
                    'include_outro': False,
                    'intro_source': 'custom',
                    'custom_intro_path': custom_intro_path,
                    'handoff': True,
                },
            )
            assert approve_res.status_code == 200
            assert approve_res.json()['approved_count'] == 2

        db_session.refresh(t1)
        db_session.refresh(t2)
        db_session.refresh(t3)

        # t1 and t2 are now attached and advanced
        assert t1.status == TalkStatus.PENDING_BOUNDS
        assert t1.custom_intro_path == f'{t1.id}/intro/intro.mp4'
        assert t1.include_intro is True
        assert t2.status == TalkStatus.PENDING_BOUNDS
        assert temp_storage.exists(f'{t1.id}/raw/raw.mp4')
        assert temp_storage.exists(f'{t2.id}/raw/raw.mp4')

        # t3 was NOT selected and remains waiting_for_files (ready for afternoon recording)
        assert t3.status == TalkStatus.WAITING_FILES
        assert not temp_storage.exists(f'{t3.id}/raw/raw.mp4')
        assert t3.room_recording_id is None
    finally:
        clip.unlink(missing_ok=True)
