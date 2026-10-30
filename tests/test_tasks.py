from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models import Job, Talk
from app.pipeline.detect import DetectResult
from app.tasks import (
    STAGE_CONFIG,
    dispatch_assembly,
    job_concat,
    job_cut,
    job_detect,
    job_ingest,
    job_intro,
    job_loudness,
    job_outro,
    job_preview,
    job_publish,
    job_transcode,
    job_waveform,
)


class MockDBContext:
    def __init__(self, talk: Talk, jobs_dict: dict[int, Job]):
        self.talk = talk
        self.jobs_dict = jobs_dict
        self.open_sessions = 0
        self.next_job_id = 100

    def __call__(self):
        ctx = self

        class Session:
            def __enter__(self_inner):
                ctx.open_sessions += 1
                session_mock = MagicMock()

                def get_mock(model, obj_id):
                    if model == Talk and obj_id == ctx.talk.id:
                        return ctx.talk
                    if model == Job and obj_id in ctx.jobs_dict:
                        return ctx.jobs_dict[obj_id]
                    return None

                def add_mock(obj):
                    if isinstance(obj, Job):
                        if obj.id is None:
                            obj.id = ctx.next_job_id
                            ctx.next_job_id += 1
                        ctx.jobs_dict[obj.id] = obj

                def query_mock(model):
                    q = MagicMock()
                    if model == Talk:

                        def talk_filter_mock(*args):
                            sub_q = MagicMock()

                            def talk_update_mock(values):
                                if "status" in values:
                                    if ctx.talk.status == "waiting_for_files":
                                        ctx.talk.status = values["status"]
                                        return 1
                                    return 0
                                return 1

                            sub_q.update.side_effect = talk_update_mock
                            sub_q.first.return_value = ctx.talk
                            return sub_q

                        q.filter.side_effect = talk_filter_mock
                        return q

                    if model == Job:

                        def filter_mock(*args):
                            sub_q = MagicMock()

                            def first_mock():
                                target_kind = None
                                for a in args:
                                    val = getattr(
                                        getattr(a, "right", None), "value", None
                                    )
                                    if val in ("intro", "outro"):
                                        target_kind = val
                                        break
                                for j in ctx.jobs_dict.values():
                                    if (
                                        j.talk_id == ctx.talk.id
                                        and j.status == "done"
                                        and (
                                            target_kind is None or j.kind == target_kind
                                        )
                                    ):
                                        return j
                                return None

                            sub_q.first.side_effect = first_mock
                            return sub_q

                        q.filter.side_effect = filter_mock
                    return q

                session_mock.get.side_effect = get_mock
                session_mock.add.side_effect = add_mock
                session_mock.query.side_effect = query_mock
                session_mock.commit = MagicMock()
                session_mock.refresh = MagicMock()
                return session_mock

            def __exit__(self_inner, exc_type, exc_val, exc_tb):
                ctx.open_sessions -= 1

        return Session()


@pytest.fixture
def dummy_talk():
    return Talk(
        id=1,
        event_id=1,
        title="Test Talk",
        room="Room A",
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status="waiting_for_files",
        cut_start=0.0,
        cut_end=1800.0,
    )


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get.return_value = Path("/tmp/fake_media.mp4")
    storage.put.return_value = None
    return storage


def test_job_ingest_success(dummy_talk, mock_storage, tmp_path):
    dummy_talk.status = "waiting_for_files"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    staged_file = tmp_path / "staged.mp4"
    staged_file.write_bytes(b"dummy video data")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.validate_media_file", return_value=None),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_ingest(dummy_talk.id, str(staged_file), f"{dummy_talk.id}/raw/raw.mp4")

    assert dummy_talk.status == "detecting"
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.kind == "ingest"
    assert mock_storage.put.call_count == 1
    mock_enqueue.assert_called_once_with(
        job_detect,
        dummy_talk.id,
        f"{dummy_talk.id}/raw/raw.mp4",
        None,
        job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
    )
    # Staged file is unlinked
    assert not staged_file.exists()


def test_job_ingest_failure_restores_waiting_for_files(
    dummy_talk, mock_storage, tmp_path
):
    dummy_talk.status = "waiting_for_files"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    staged_file = tmp_path / "staged.mp4"
    staged_file.write_bytes(b"corrupt video data")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch(
            "app.tasks.validate_media_file",
            side_effect=ValueError("Invalid video stream"),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
        pytest.raises(ValueError, match="Invalid video stream"),
    ):
        job_ingest(dummy_talk.id, str(staged_file))

    assert dummy_talk.status == "waiting_for_files"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    mock_enqueue.assert_not_called()
    assert not staged_file.exists()


def test_job_ingest_stale_or_concurrent_discarded(dummy_talk, mock_storage, tmp_path):
    # If talk is already detecting or past waiting_for_files, job_ingest discards without altering status
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    staged_file = tmp_path / "staged.mp4"
    staged_file.write_bytes(b"dummy video data")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.validate_media_file") as mock_validate,
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_ingest(dummy_talk.id, str(staged_file))

    assert dummy_talk.status == "detecting"
    assert len(jobs) == 0
    mock_validate.assert_not_called()
    mock_storage.put.assert_not_called()
    mock_enqueue.assert_not_called()
    assert not staged_file.exists()


def test_no_db_session_held_during_detect(dummy_talk, mock_storage):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_detect(raw_path, scheduled_start, scheduled_end, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during detect!"
        return DetectResult(
            passed=True,
            actual_duration_seconds=1800.0,
            has_video=True,
            has_audio=True,
            reason=None,
        )

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", side_effect=fake_detect),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_detect(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "pending_approval"
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.kind == "detect"
    mock_enqueue.assert_not_called()  # Halts at pending_approval


def test_detect_failure_advances_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch(
            "app.tasks.detect",
            return_value=DetectResult(
                passed=False,
                actual_duration_seconds=10.0,
                has_video=False,
                has_audio=False,
                reason="duration too short",
            ),
        ),
        pytest.raises(ValueError, match="Detection failed"),
    ):
        job_detect(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "broken"
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.log_path == f"1/logs/job_{job.id}.log"
    mock_storage.put.assert_called_once()


def test_failure_on_already_broken_talk_does_not_crash(dummy_talk, mock_storage):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_detect(raw_path, scheduled_start, scheduled_end, **kwargs):
        dummy_talk.status = "broken"
        raise RuntimeError("Unexpected error")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", side_effect=fake_detect),
        pytest.raises(RuntimeError, match="Unexpected error"),
    ):
        job_detect(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "broken"
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert job.status == "failed"


def test_failure_on_done_talk_does_not_transition_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_detect(raw_path, scheduled_start, scheduled_end, **kwargs):
        dummy_talk.status = "done"
        raise RuntimeError("Late job error")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", side_effect=fake_detect),
        pytest.raises(RuntimeError, match="Late job error"),
    ):
        job_detect(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "done"
    job = next(iter(jobs.values()))
    assert job.status == "failed"


def test_failure_on_rejected_talk_does_not_transition_to_broken(
    dummy_talk, mock_storage
):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_detect(raw_path, scheduled_start, scheduled_end, **kwargs):
        dummy_talk.status = "rejected"
        raise RuntimeError("Late job error")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", side_effect=fake_detect),
        pytest.raises(RuntimeError, match="Late job error"),
    ):
        job_detect(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "rejected"
    job = next(iter(jobs.values()))
    assert job.status == "failed"


def test_failure_when_storage_put_raises_exception(dummy_talk, mock_storage):
    dummy_talk.status = "cutting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)
    mock_storage.put.side_effect = RuntimeError("Storage cluster unreachable")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=RuntimeError("Cut failed")),
        pytest.raises(RuntimeError, match="Cut failed"),
    ):
        job_cut(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"


def test_no_db_session_held_during_cut_and_enqueues_preview(dummy_talk, mock_storage):
    dummy_talk.status = "cutting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_cut(input_path, output_path, start_s, end_s, *args, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during cut!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_cut(1, "1/raw/raw.mp4", "1/cut/cut.mp4")

    assert dummy_talk.status == "generating_previews"
    job = next(iter(jobs.values()))
    assert job.status == "done"
    mock_enqueue.assert_called_once_with(
        job_preview,
        1,
        "1/cut/cut.mp4",
        "1/preview/preview.mp4",
        job_timeout=STAGE_CONFIG["preview"]["job_timeout"],
    )


def test_no_db_session_held_during_intro(dummy_talk, mock_storage):
    dummy_talk.status = "generating_previews"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_generate_intro_clip(output_path, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during intro generation!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_intro_clip", side_effect=fake_generate_intro_clip),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_intro(1, "1/intro/intro.mp4")

    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.kind == "intro"
    mock_enqueue.assert_not_called()


def test_intro_exception_leads_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "generating_previews"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch(
            "app.tasks.generate_intro_clip",
            side_effect=RuntimeError("Intro rendering failed"),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
        pytest.raises(RuntimeError, match="Intro rendering failed"),
    ):
        job_intro(1)

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "intro"
    assert job.log_path is not None
    mock_enqueue.assert_not_called()


def test_no_db_session_held_during_outro(dummy_talk, mock_storage):
    dummy_talk.status = "generating_previews"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_generate_outro_clip(output_path, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during outro generation!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_outro_clip", side_effect=fake_generate_outro_clip),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_outro(1, "1/outro/outro.mp4")

    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.kind == "outro"
    mock_enqueue.assert_not_called()


def test_outro_exception_leads_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "generating_previews"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch(
            "app.tasks.generate_outro_clip",
            side_effect=RuntimeError("Outro rendering failed"),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
        pytest.raises(RuntimeError, match="Outro rendering failed"),
    ):
        job_outro(1)

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "outro"
    assert job.log_path is not None
    mock_enqueue.assert_not_called()


def test_cut_exception_leads_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "cutting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=RuntimeError("FFmpeg cut failed")),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
        pytest.raises(RuntimeError, match="FFmpeg cut failed"),
    ):
        job_cut(1, "1/raw/raw.mp4")

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.log_path is not None
    mock_enqueue.assert_not_called()


def test_no_db_session_held_during_preview_and_halts(dummy_talk, mock_storage):
    dummy_talk.status = "generating_previews"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_preview(input_path, output_path, preset, *args, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during preview!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_preview", side_effect=fake_preview),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_preview(1, "1/cut/cut.mp4", "1/preview/preview.mp4")

    assert dummy_talk.status == "preview"
    job = next(iter(jobs.values()))
    assert job.status == "done"
    mock_enqueue.assert_not_called()  # Halts at preview for human review


def test_loudness_enqueues_transcode_on_heavy(dummy_talk, mock_storage):
    dummy_talk.status = "assembling"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_loudness(input_path, output_path):
        assert db_ctx.open_sessions == 0, "DB session was open during loudness!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize", side_effect=fake_loudness),
        patch("app.tasks.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        job_loudness(1, "1/assemble/assemble.mp4", "1/assemble/assemble_loud.mp4")

    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.kind == "loudness"
    assert dummy_talk.status == "transcoding"
    mock_heavy_enqueue.assert_called_once_with(
        job_transcode,
        1,
        "1/assemble/assemble_loud.mp4",
        "1/final/final.mp4",
        job_timeout=STAGE_CONFIG["transcode"]["job_timeout"],
    )


def test_loudness_failure_advances_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "assembling"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch(
            "app.tasks.normalize",
            side_effect=RuntimeError("Loudness normalization failed"),
        ),
        patch("app.tasks.heavy_queue.enqueue") as mock_heavy_enqueue,
        pytest.raises(RuntimeError, match="Loudness normalization failed"),
    ):
        job_loudness(1, "1/assemble/assemble.mp4", "1/assemble/assemble_loud.mp4")

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "loudness"
    assert job.log_path is not None
    mock_heavy_enqueue.assert_not_called()


def test_transcode_enqueues_publish_on_light(dummy_talk, mock_storage):
    dummy_talk.status = "transcoding"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_transcode(input_path, output_path, on_progress=None, *args, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during transcode!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.transcode", side_effect=fake_transcode),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        job_transcode(1, "1/cut/cut_loud.mp4", "1/final/final.mp4")

    assert dummy_talk.status == "uploading"
    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.progress_pct == 100.0
    mock_light_enqueue.assert_called_once_with(
        job_publish,
        1,
        "1/final/final.mp4",
        job_timeout=STAGE_CONFIG["publish"]["job_timeout"],
    )


def test_transcode_cleans_up_bumpers(dummy_talk, mock_storage, tmp_path):
    import uuid

    from app.ingest import get_bumper_staging_dir

    dummy_talk.status = "transcoding"
    staging_dir = get_bumper_staging_dir()
    staged_bumper = staging_dir / f"test_staged_{uuid.uuid4().hex}.mp4"
    staged_bumper.write_bytes(b"dummy")

    non_staged_bumper = tmp_path / "custom_outro.mp4"
    non_staged_bumper.write_bytes(b"keep_me")

    dummy_talk.custom_intro_path = str(staged_bumper)
    dummy_talk.custom_outro_path = str(non_staged_bumper)
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.transcode"),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_transcode(1, "1/cut/cut_loud.mp4", "1/final/final.mp4")

    assert not staged_bumper.exists()
    assert non_staged_bumper.exists()
    mock_storage.delete.assert_any_call("1/intro")
    mock_storage.delete.assert_any_call("1/outro")


def test_transcode_progress_callback_updates_job_progress(dummy_talk, mock_storage):
    dummy_talk.status = "transcoding"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    progress_history: list[float] = []

    def fake_transcode(input_path, output_path, on_progress=None, *args, **kwargs):
        assert db_ctx.open_sessions == 0, "DB session was open during transcode!"
        if on_progress:
            on_progress(0.25)
            assert db_ctx.open_sessions == 0
            job = next(iter(jobs.values()))
            progress_history.append(job.progress_pct)
            on_progress(0.60)
            assert db_ctx.open_sessions == 0
            progress_history.append(job.progress_pct)
            on_progress(0.95)
            assert db_ctx.open_sessions == 0
            progress_history.append(job.progress_pct)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.transcode", side_effect=fake_transcode),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_transcode(
            1,
            "1/cut/cut_loud.mp4",
            "1/final/final.mp4",
            progress_throttle_s=0.0,
        )

    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.progress_pct == 100.0
    assert 25.0 in progress_history
    assert 60.0 in progress_history
    assert 95.0 in progress_history


def test_publish_advances_to_done_and_halts(dummy_talk, mock_storage):
    dummy_talk.status = "uploading"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_publish(local_path, talk_id, backend):
        assert db_ctx.open_sessions == 0, "DB session was open during publish!"
        assert talk_id == 1
        assert backend == mock_storage

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.publish", side_effect=fake_publish) as mock_pub,
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_publish(1, "1/final/final.mp4")

    mock_pub.assert_called_once()
    assert dummy_talk.status == "done"
    job = next(iter(jobs.values()))
    assert job.status == "done"
    mock_enqueue.assert_not_called()  # Terminal state
    assert mock_storage.delete.call_count == 5
    for stage in ("1/cut", "1/preview", "1/assemble", "1/intro", "1/outro"):
        mock_storage.delete.assert_any_call(stage)


def test_publish_exception_leads_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "uploading"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.publish", side_effect=RuntimeError("Publish failed")),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
        pytest.raises(RuntimeError, match="Publish failed"),
    ):
        job_publish(1, "1/final/final.mp4")

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "publish"
    assert job.log_path is not None
    mock_enqueue.assert_not_called()
    mock_storage.delete.assert_not_called()


def test_publish_storage_delete_resilient(dummy_talk, mock_storage):
    dummy_talk.status = "uploading"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)
    mock_storage.delete.side_effect = RuntimeError("Disk failure")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.publish", return_value=None),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_publish(1, "1/final/final.mp4")

    assert dummy_talk.status == "done"
    job = next(iter(jobs.values()))
    assert job.status == "done"
    mock_enqueue.assert_not_called()
    assert mock_storage.delete.call_count == 5
    for stage in ("1/cut", "1/preview", "1/assemble", "1/intro", "1/outro"):
        mock_storage.delete.assert_any_call(stage)


def test_job_detect_discards_when_talk_aborted(dummy_talk, mock_storage):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_detect(raw_path, scheduled_start, scheduled_end, **kwargs):
        # Simulate /abort occurring during pure video processing
        dummy_talk.status = "waiting_for_files"
        jobs.clear()
        return DetectResult(
            passed=True,
            actual_duration_seconds=1800.0,
            has_video=True,
            has_audio=True,
            reason=None,
        )

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", side_effect=fake_detect),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_detect(1, "1/raw/raw.mp4")

    # Status remains waiting_for_files and no further transitions occurred
    assert dummy_talk.status == "waiting_for_files"
    mock_enqueue.assert_not_called()


def test_job_detect_discards_when_talk_in_waiting_for_files(dummy_talk, mock_storage):
    dummy_talk.status = "waiting_for_files"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_detect(1, "1/raw/raw.mp4")

    # Job discarded prior to processing; no db records created and no detection executed
    assert dummy_talk.status == "waiting_for_files"
    assert len(jobs) == 0
    mock_detect.assert_not_called()
    mock_enqueue.assert_not_called()


def test_job_detect_discards_when_talk_not_found(dummy_talk, mock_storage):
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_detect(999, "999/raw/raw.mp4")

    assert len(jobs) == 0
    mock_detect.assert_not_called()
    mock_enqueue.assert_not_called()


def test_job_cut_discards_when_talk_aborted(dummy_talk, mock_storage):
    dummy_talk.status = "cutting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_cut(input_path, output_path, start_s, end_s, *args, **kwargs):
        # Simulate /abort occurring during pure video processing
        dummy_talk.status = "waiting_for_files"
        jobs.clear()

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_cut(1, "1/raw/raw.mp4", "1/cut/cut.mp4")

    # Talk should not have been advanced to generating_previews or broken
    assert dummy_talk.status == "waiting_for_files"
    mock_enqueue.assert_not_called()


def test_job_cut_with_local_storage_caches_waveform(dummy_talk, tmp_path):
    import json

    from app.storage import LocalDiskBackend
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(
        2.0,
        has_video=True,
        has_audio=True,
        audio_waveform="tone",
        output_dir=tmp_path / "raw_src",
    )
    storage.put("1/raw/raw.mp4", raw_clip)

    dummy_talk.status = "cutting"
    dummy_talk.cut_start = 0.0
    dummy_talk.cut_end = 2.0
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=storage),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4", "1/cut/cut.mp4")

    assert storage.exists("1/cut/cut.mp4")
    wf_key = "1/cut/cut.mp4.waveform.json"
    assert storage.exists(wf_key)
    wf_content = json.loads(storage.get(wf_key).read_text(encoding="utf-8"))
    assert len(wf_content["peaks"]) > 0
    assert max(wf_content["peaks"]) > 0.0


def test_job_cut_multi_segment_concatenates_cuts(dummy_talk, tmp_path):
    import math

    import av
    import numpy as np

    from app.pipeline.detect import container_duration_seconds
    from app.storage import LocalDiskBackend

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_path = tmp_path / "raw_src" / "raw.mp4"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate distinguishable 3.0s media: 1s Red, 1s Green, 1s Blue
    fps = 24
    with av.open(str(raw_path), mode="w") as c:
        v = c.add_stream("libx264", rate=fps)
        v.width = 64
        v.height = 64
        v.pix_fmt = "yuv420p"
        a = c.add_stream("aac", rate=44100)
        a.layout = "mono"

        for i in range(3 * fps):
            color = (
                (255, 0, 0)
                if i < fps
                else ((0, 255, 0) if i < 2 * fps else (0, 0, 255))
            )
            arr = np.full((64, 64, 3), color, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for pkt in v.encode(frame):
                c.mux(pkt)
        for pkt in v.encode():
            c.mux(pkt)

        silence = np.zeros(1024, dtype=np.int16).reshape(1, -1)
        for _ in range(math.ceil(3.0 * 44100 / 1024)):
            a_frame = av.AudioFrame.from_ndarray(silence, format="s16", layout="mono")
            a_frame.sample_rate = 44100
            for pkt in a.encode(a_frame):
                c.mux(pkt)
        for pkt in a.encode():
            c.mux(pkt)

    storage.put("1/raw/raw.mp4", raw_path)

    dummy_talk.status = "cutting"
    dummy_talk.raw_duration_seconds = 3.0
    dummy_talk.cut_start = 0.0
    dummy_talk.cut_end = 3.0
    # Select [0.0, 0.8] (red) and [2.2, 3.0] (blue); exclude [0.8, 2.2] (green)
    dummy_talk.cut_segments = [
        {"start": 0.0, "end": 0.8},
        {"start": 2.2, "end": 3.0},
    ]
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=storage),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4", "1/cut/cut.mp4")

    assert storage.exists("1/cut/cut.mp4")
    out_file = storage.get("1/cut/cut.mp4")

    observed_colors = []
    with av.open(str(out_file)) as c:
        out_dur = container_duration_seconds(c)
        for frame in c.decode(video=0):
            rgb = frame.to_ndarray(format="rgb24")
            mean_c = rgb.mean(axis=(0, 1))
            r, g, b = mean_c[0], mean_c[1], mean_c[2]
            if r > 150 and g < 80 and b < 80:
                c_name = "red"
            elif g > 150 and r < 80 and b < 80:
                c_name = "green"
            elif b > 150 and r < 80 and g < 80:
                c_name = "blue"
            else:
                c_name = "other"
            if not observed_colors or observed_colors[-1] != c_name:
                observed_colors.append(c_name)

    assert out_dur == pytest.approx(1.6, abs=0.5)
    assert "green" not in observed_colors, (
        "Intervening footage (green) was not removed!"
    )
    assert observed_colors == ["red", "blue"], (
        f"Expected red followed by blue, got: {observed_colors}"
    )


def test_job_cut_discarded_if_aborted_or_superseded(dummy_talk, tmp_path):
    from app.storage import LocalDiskBackend
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(2.0, output_dir=tmp_path / "raw_src")
    storage.put("1/raw/raw.mp4", raw_clip)
    # Pre-populate cut.mp4 with content from replacement cut
    storage.put("1/cut/cut.mp4", b"replacement cut content")

    dummy_talk.status = "cutting"
    dummy_talk.raw_duration_seconds = 2.0
    dummy_talk.cut_start = 0.0
    dummy_talk.cut_end = 1.0
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    # Simulate talk aborted / state changed to waiting_for_files during cut
    def fake_cut(inp, out, *args, **kwargs):
        # Organizer aborts while cut was executing
        dummy_talk.status = "waiting_for_files"
        Path(out).write_bytes(b"stale worker output")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4", "1/cut/cut.mp4")

    # Stale worker output must NOT overwrite replacement cut
    assert storage.get("1/cut/cut.mp4").read_bytes() == b"replacement cut content"
    assert dummy_talk.status == "waiting_for_files"


def test_job_cut_resynchronizes_stale_cut_segments(dummy_talk, tmp_path):
    from app.storage import LocalDiskBackend
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(2.0, output_dir=tmp_path / "raw_src")
    storage.put("1/raw/raw.mp4", raw_clip)

    dummy_talk.status = "cutting"
    dummy_talk.raw_duration_seconds = 2.0
    # Simulate single-range resubmission from older instance:
    dummy_talk.cut_start = 0.2
    dummy_talk.cut_end = 0.8
    # cut_segments still contains previous multi-segment or mismatched range:
    dummy_talk.cut_segments = [
        {"start": 1.0, "end": 1.5},
        {"start": 1.6, "end": 1.9},
    ]
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    captured_bounds = []

    def fake_cut(inp, out, start, end, *args, **kwargs):
        captured_bounds.append((start, end))
        Path(out).write_bytes(b"cut content")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4", "1/cut/cut.mp4")

    # job_cut detected stale segments and resynchronized with scalar bounds
    assert captured_bounds == [(0.2, 0.8)]
    assert dummy_talk.cut_segments == [{"start": 0.2, "end": 0.8}]


def test_job_cut_multi_segment_enforces_frame_accurate_fallback(dummy_talk, tmp_path):
    from app.storage import LocalDiskBackend
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(3.0, output_dir=tmp_path / "raw_src")
    storage.put("1/raw/raw.mp4", raw_clip)

    dummy_talk.status = "cutting"
    dummy_talk.raw_duration_seconds = 3.0
    dummy_talk.cut_start = 0.5
    dummy_talk.cut_end = 2.5
    dummy_talk.cut_segments = [
        {"start": 0.5, "end": 1.2},
        {"start": 1.8, "end": 2.5},
    ]
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    cut_calls = []

    def fake_cut(inp, out, start, end, *args, **kwargs):
        cut_calls.append((start, end, kwargs))
        Path(out).write_bytes(b"cut segment")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch(
            "app.tasks.concat_segments",
            side_effect=lambda segs, out, **kw: Path(out).write_bytes(b"concatenated"),
        ),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4", "1/cut/cut.mp4")

    assert len(cut_calls) == 2
    for call in cut_calls:
        assert call[2].get("frame_accurate_fallback") is True


def test_handle_failure_on_deleted_job_does_not_mark_talk_broken(
    dummy_talk, mock_storage
):
    dummy_talk.status = "waiting_for_files"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    from app.tasks import _handle_failure

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
    ):
        _handle_failure(1, 999, RuntimeError("Aborted failure"), mock_storage)

    assert dummy_talk.status == "waiting_for_files"


def test_job_concat_enqueues_loudness_on_light(dummy_talk, mock_storage):
    dummy_talk.status = "assembling"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_concat(cut_path, intro_path, outro_path, output_path, backend):
        assert db_ctx.open_sessions == 0, "DB session was open during concat!"

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat", side_effect=fake_concat) as mock_concat,
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        job_concat(
            1,
            cut_key="1/cut/custom_cut.mp4",
            intro_key="1/intro/intro.mp4",
            outro_key="1/outro/outro.mp4",
            concat_key="1/assemble/assemble.mp4",
        )

    assert dummy_talk.status == "assembling"
    job = next(iter(jobs.values()))
    assert job.status == "done"
    assert job.kind == "concat"
    mock_concat.assert_called_once()
    mock_light_enqueue.assert_called_once_with(
        job_loudness,
        1,
        "1/assemble/assemble.mp4",
        "1/assemble/assemble_loud.mp4",
        job_timeout=STAGE_CONFIG["loudness"]["job_timeout"],
    )


def test_job_concat_failure_advances_to_broken(dummy_talk, mock_storage):
    dummy_talk.status = "assembling"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat", side_effect=RuntimeError("PyAV concat failure")),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
        pytest.raises(RuntimeError, match="PyAV concat failure"),
    ):
        job_concat(1, "1/cut/cut.mp4")

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "concat"
    assert job.log_path is not None
    mock_light_enqueue.assert_not_called()


def test_job_concat_missing_intro_key_raises_and_marks_broken(dummy_talk, mock_storage):
    dummy_talk.status = "assembling"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_get(key: str) -> Path:
        if key == "1/intro/nonexistent.mp4":
            raise FileNotFoundError(f"Key not found in storage: {key}")
        return Path("/tmp/fake_media.mp4")

    mock_storage.get.side_effect = fake_get

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat") as mock_concat,
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
        pytest.raises(FileNotFoundError, match="1/intro/nonexistent.mp4"),
    ):
        job_concat(
            1,
            cut_key="1/cut/cut.mp4",
            intro_key="1/intro/nonexistent.mp4",
        )

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "concat"
    mock_concat.assert_not_called()
    mock_light_enqueue.assert_not_called()


def test_job_concat_missing_outro_key_raises_and_marks_broken(dummy_talk, mock_storage):
    dummy_talk.status = "assembling"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_get(key: str) -> Path:
        if key == "1/outro/nonexistent.mp4":
            raise FileNotFoundError(f"Key not found in storage: {key}")
        return Path("/tmp/fake_media.mp4")

    mock_storage.get.side_effect = fake_get

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat") as mock_concat,
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
        pytest.raises(FileNotFoundError, match="1/outro/nonexistent.mp4"),
    ):
        job_concat(
            1,
            cut_key="1/cut/cut.mp4",
            outro_key="1/outro/nonexistent.mp4",
        )

    assert dummy_talk.status == "broken"
    job = next(iter(jobs.values()))
    assert job.status == "failed"
    assert job.kind == "concat"
    mock_concat.assert_not_called()
    mock_light_enqueue.assert_not_called()


@pytest.mark.parametrize(
    ("include_intro", "include_outro"),
    [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_concat_to_loudness_to_transcode_all_combinations(
    dummy_talk, mock_storage, include_intro, include_outro
):
    dummy_talk.status = "assembling"
    dummy_talk.include_intro = include_intro
    dummy_talk.include_outro = include_outro
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def fake_concat(cut_path, intro_path, outro_path, output_path, backend):
        assert db_ctx.open_sessions == 0, "DB session open during concat!"

    def fake_loudness(input_path, output_path):
        assert db_ctx.open_sessions == 0, "DB session open during loudness!"

    intro_key = "1/intro/intro.mp4" if include_intro else None
    outro_key = "1/outro/outro.mp4" if include_outro else None
    cut_key = "1/cut/cut.mp4"
    concat_key = "1/assemble/assemble.mp4"
    loud_key = "1/assemble/assemble_loud.mp4"
    final_key = "1/final/final.mp4"

    # Step 1: Run job_concat
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat", side_effect=fake_concat) as mock_concat,
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        job_concat(
            1,
            cut_key=cut_key,
            intro_key=intro_key,
            outro_key=outro_key,
            concat_key=concat_key,
        )

    assert dummy_talk.status == "assembling"
    mock_concat.assert_called_once()
    mock_light_enqueue.assert_called_once_with(
        job_loudness,
        1,
        concat_key,
        loud_key,
        job_timeout=STAGE_CONFIG["loudness"]["job_timeout"],
    )

    # Step 2: Run job_loudness on concat's output
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize", side_effect=fake_loudness) as mock_norm,
        patch("app.tasks.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        job_loudness(1, cut_key=concat_key, loud_key=loud_key)

    mock_norm.assert_called_once()
    assert dummy_talk.status == "transcoding"
    mock_heavy_enqueue.assert_called_once_with(
        job_transcode,
        1,
        loud_key,
        final_key,
        job_timeout=STAGE_CONFIG["transcode"]["job_timeout"],
    )


def test_dispatch_assembly_skip_both_enqueues_concat(dummy_talk):
    dummy_talk.status = "assembling"
    dummy_talk.include_intro = False
    dummy_talk.include_outro = False
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        dispatch_assembly(1, cut_key="1/cut/special.mp4")

    mock_light_enqueue.assert_called_once_with(
        job_concat,
        1,
        "1/cut/special.mp4",
        None,
        None,
        "1/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_dispatch_assembly_generated_intro_enqueues_job_intro(dummy_talk):
    dummy_talk.status = "assembling"
    dummy_talk.include_intro = True
    dummy_talk.intro_source = "generated"
    dummy_talk.include_outro = False
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        dispatch_assembly(1, cut_key="1/cut/special.mp4")

    mock_light_enqueue.assert_called_once_with(
        job_intro,
        1,
        "1/cut/special.mp4",
        "1/intro/intro.mp4",
        job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
    )


def test_dispatch_assembly_intro_done_outro_generated_enqueues_job_outro(dummy_talk):
    dummy_talk.status = "assembling"
    dummy_talk.include_intro = True
    dummy_talk.intro_source = "generated"
    dummy_talk.include_outro = True
    dummy_talk.outro_source = "generated"
    intro_job = Job(id=50, talk_id=1, kind="intro", status="done")
    jobs = {50: intro_job}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        dispatch_assembly(1, cut_key="1/cut/special.mp4")

    mock_light_enqueue.assert_called_once_with(
        job_outro,
        1,
        "1/cut/special.mp4",
        "1/outro/outro.mp4",
        job_timeout=STAGE_CONFIG["outro"]["job_timeout"],
    )


def test_job_waveform_skips_if_waveform_already_exists():
    mock_storage = MagicMock()
    mock_storage.exists.side_effect = lambda key: key.endswith(".waveform.json")

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_waveform(1, "1/preview/preview.mp4")

    mock_cache.assert_not_called()
    mock_storage.get.assert_not_called()


def test_job_waveform_skips_if_media_key_missing():
    mock_storage = MagicMock()
    mock_storage.exists.return_value = False

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_waveform(1, "1/preview/preview.mp4")

    mock_cache.assert_not_called()
    mock_storage.get.assert_not_called()


def test_job_waveform_handles_storage_key_not_found_on_get():
    from app.storage import StorageKeyNotFoundError

    mock_storage = MagicMock()

    def exists_side_effect(key):
        return not key.endswith(".waveform.json")

    mock_storage.exists.side_effect = exists_side_effect
    mock_storage.get.side_effect = StorageKeyNotFoundError("preview.mp4")

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        # Should not raise StorageKeyNotFoundError
        job_waveform(1, "1/preview/preview.mp4")

    mock_cache.assert_not_called()


def test_job_waveform_generates_when_needed():
    mock_storage = MagicMock()

    def exists_side_effect(key):
        return not key.endswith(".waveform.json")

    mock_storage.exists.side_effect = exists_side_effect
    mock_storage.get.return_value = Path("/tmp/fake_preview.mp4")

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_waveform(1, "1/preview/preview.mp4")

    mock_storage.get.assert_called_once_with("1/preview/preview.mp4")
    mock_cache.assert_called_once_with(
        mock_storage, "1/preview/preview.mp4", Path("/tmp/fake_preview.mp4")
    )


def test_job_preview_reuses_cached_cut_waveform(dummy_talk, mock_storage):
    dummy_talk.status = "generating_previews"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    def exists_side_effect(key):
        return key == "1/cut/cut.mp4.waveform.json"

    mock_storage.exists.side_effect = exists_side_effect
    mock_path = MagicMock()
    mock_path.read_bytes.return_value = b'{"peaks": [0.5]}'
    mock_storage.get.return_value = mock_path

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_preview"),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_preview(1, "1/cut/cut.mp4", "1/preview/preview.mp4")

    mock_storage.put.assert_any_call(
        "1/preview/preview.mp4.waveform.json", b'{"peaks": [0.5]}'
    )
    mock_cache.assert_not_called()


def test_encoder_threads_forwarded_when_configured(
    dummy_talk, mock_storage, monkeypatch
):
    dummy_talk.status = "cutting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    from app.config import settings

    monkeypatch.setattr(settings, "encoder_threads", 2)

    captured_cut_kwargs = {}

    def fake_cut(*args, **kwargs):
        captured_cut_kwargs.update(kwargs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_cut(1, "1/raw/raw.mp4", "1/cut/cut.mp4")

    assert captured_cut_kwargs.get("threads") == 2

    # Verify job_preview forwards threads=2
    dummy_talk.status = "generating_previews"
    captured_preview_kwargs = {}

    def fake_preview(*args, **kwargs):
        captured_preview_kwargs.update(kwargs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_preview", side_effect=fake_preview),
        patch("app.tasks._cache_waveform"),
    ):
        job_preview(1, "1/cut/cut.mp4", "1/preview/preview.mp4")

    assert captured_preview_kwargs.get("threads") == 2

    # Verify job_transcode forwards threads=2
    dummy_talk.status = "transcoding"
    captured_transcode_kwargs = {}

    def fake_transcode(*args, **kwargs):
        captured_transcode_kwargs.update(kwargs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.transcode", side_effect=fake_transcode),
        patch("app.tasks._cache_waveform"),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_transcode(1, "1/cut/cut_loud.mp4", "1/final/final.mp4")

    assert captured_transcode_kwargs.get("threads") == 2


def test_job_detect_room_recording_tolerance_override(dummy_talk, mock_storage):
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
    ):
        mock_detect.return_value = DetectResult(
            passed=True,
            actual_duration_seconds=12600.0,
            has_video=True,
            has_audio=True,
            reason=None,
        )
        job_detect(1, "1/raw/raw.mp4", tolerance_seconds=float("inf"))

        mock_detect.assert_called_once_with(
            mock_storage.get("1/raw/raw.mp4"),
            scheduled_start=dummy_talk.start,
            scheduled_end=dummy_talk.end,
            tolerance_seconds=float("inf"),
        )
        assert dummy_talk.status == "pending_approval"
        assert dummy_talk.raw_duration_seconds == 12600.0


def test_job_detect_recording_start_alone_retains_configured_tolerance(
    dummy_talk, mock_storage
):
    """A timestamp alone (recording_start) must NOT set infinite tolerance for ordinary recordings."""
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
    ):
        mock_detect.return_value = DetectResult(
            passed=True,
            actual_duration_seconds=1800.0,
            has_video=True,
            has_audio=True,
            reason=None,
        )
        rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
        job_detect(1, "1/raw/raw.mp4", recording_start=rec_start)

        # Configured default tolerance (300.0) is retained, not inf
        mock_detect.assert_called_once_with(
            mock_storage.get("1/raw/raw.mp4"),
            scheduled_start=dummy_talk.start,
            scheduled_end=dummy_talk.end,
        )


def test_job_detect_confirmed_room_recording_bypasses_duration(
    dummy_talk, mock_storage
):
    """When is_room_recording=True, infinite tolerance is applied and forwarded."""
    dummy_talk.status = "detecting"
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
    ):
        mock_detect.return_value = DetectResult(
            passed=True,
            actual_duration_seconds=12600.0,
            has_video=True,
            has_audio=True,
            reason=None,
        )
        rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
        job_detect(
            1, "1/raw/raw.mp4", recording_start=rec_start, is_room_recording=True
        )

        mock_detect.assert_called_once_with(
            mock_storage.get("1/raw/raw.mp4"),
            scheduled_start=dummy_talk.start,
            scheduled_end=dummy_talk.end,
            tolerance_seconds=float("inf"),
            is_room_recording=True,
        )


def test_unified_transcode_resolves_staged_slates_and_bypasses_concat_reencode(
    dummy_talk, mock_storage
):
    """Verify job_concat avoids re-encoding slates on light worker and job_transcode resolves them for single pass."""
    dummy_talk.status = "assembling"
    dummy_talk.include_intro = True
    dummy_talk.include_outro = True
    jobs = {}
    db_ctx = MockDBContext(dummy_talk, jobs)

    captured_concat_kwargs = {}

    def fake_concat(cut_path, intro_path, outro_path, output_path, **kwargs):
        captured_concat_kwargs["cut_path"] = cut_path
        captured_concat_kwargs["intro_path"] = intro_path
        captured_concat_kwargs["outro_path"] = outro_path
        captured_concat_kwargs["output_path"] = output_path

    captured_transcode_kwargs = {}

    def fake_transcode(input_path, output_path, **kwargs):
        captured_transcode_kwargs["input_path"] = input_path
        captured_transcode_kwargs["output_path"] = output_path
        captured_transcode_kwargs.update(kwargs)

    # 1. job_concat: validates slates but passes cut alone to concat()
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat", side_effect=fake_concat),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_concat(
            1,
            cut_key="1/cut/cut.mp4",
            intro_key="1/intro/intro.mp4",
            outro_key="1/outro/outro.mp4",
            concat_key="1/assemble/assemble.mp4",
        )

    assert captured_concat_kwargs["intro_path"] is None
    assert captured_concat_kwargs["outro_path"] is None
    assert captured_concat_kwargs["output_path"] == "1/assemble/assemble.mp4"

    # 2. job_transcode: resolves staged slates and unifies them in single transcode pass
    dummy_talk.status = "transcoding"
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.transcode", side_effect=fake_transcode),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_transcode(1, "1/assemble/assemble_loud.mp4", "1/final/final.mp4")

    assert captured_transcode_kwargs["intro_path"] == Path("/tmp/fake_media.mp4")
    assert captured_transcode_kwargs["outro_path"] == Path("/tmp/fake_media.mp4")
    assert captured_transcode_kwargs["target_lufs"] is None


def test_job_ingest_room_url_success(tmp_path):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock, patch

    from app.models import Job, Talk
    from app.tasks import job_ingest_room_url
    from tests.conftest import generate_clip

    clip = generate_clip(1.0, output_dir=tmp_path)
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)

    talk1 = Talk(
        id=201,
        event_id=10,
        title="Talk 1",
        room="Room URL",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    talk2 = Talk(
        id=202,
        event_id=10,
        title="Talk 2",
        room="Room URL",
        start=now + timedelta(minutes=30),
        end=now + timedelta(minutes=60),
        status="waiting_for_files",
    )

    talks_dict = {201: talk1, 202: talk2}
    jobs_dict = {}

    class RoomMockDBSession:
        def __enter__(self):
            s = MagicMock()

            def query_mock(model):
                q = MagicMock()
                if model == Talk:

                    def filter_mock(*args):
                        sub = MagicMock()
                        sub.filter.return_value = sub
                        sub.order_by.return_value = sub
                        sub.all.return_value = list(talks_dict.values())
                        return sub

                    q.filter.side_effect = filter_mock
                return q

            s.query.side_effect = query_mock
            s.get.side_effect = lambda m, pk: (
                talks_dict.get(pk) if m == Talk else jobs_dict.get(pk)
            )

            def add_mock(obj):
                if isinstance(obj, Job):
                    obj.id = len(jobs_dict) + 1
                    jobs_dict[obj.id] = obj

            s.add.side_effect = add_mock
            return s

        def __exit__(self, *args):
            pass

    mock_storage = MagicMock()
    mock_storage.free_bytes.return_value = 10**10

    with (
        patch("app.tasks.SessionLocal", side_effect=RoomMockDBSession),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.download_media_url", return_value=clip),
        patch("app.tasks.container_duration_seconds", return_value=3600.0),
        patch("app.tasks.light_queue.enqueue") as mock_light_q,
    ):
        job_ingest_room_url(
            event_id=10,
            room="Room URL",
            video_url="https://example.com/recordings/sample.mp4",
            recording_start_iso=now.isoformat(),
            talk_ids=[201, 202],
        )

    assert talk1.status == "detecting"
    assert talk2.status == "detecting"
    assert talk1.cut_start == 0.0
    assert talk1.cut_end == 1800.0
    assert talk2.cut_start == 1800.0
    assert talk2.cut_end == 3600.0
    assert mock_storage.link_or_copy.call_count == 2
    assert mock_light_q.call_count == 2


def test_job_ingest_room_url_download_failure_handles_cleanup(tmp_path):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock, patch

    from app.models import Job, Talk
    from app.tasks import job_ingest_room_url

    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    talk = Talk(
        id=205,
        event_id=10,
        title="Talk Fail",
        room="Room URL",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    talks_dict = {205: talk}
    jobs_dict = {}

    class RoomMockDBSession:
        def __enter__(self):
            s = MagicMock()

            def query_mock(model):
                q = MagicMock()
                if model == Talk:

                    def filter_mock(*args):
                        sub = MagicMock()
                        sub.filter.return_value = sub
                        sub.order_by.return_value = sub
                        sub.all.return_value = list(talks_dict.values())
                        return sub

                    q.filter.side_effect = filter_mock
                return q

            s.query.side_effect = query_mock

            def add_mock(instance):
                if isinstance(instance, Job):
                    jid = len(jobs_dict) + 1
                    instance.id = jid
                    jobs_dict[jid] = instance

            s.add.side_effect = add_mock
            s.get.side_effect = lambda model, obj_id: (
                jobs_dict.get(obj_id) if model == Job else talks_dict.get(obj_id)
            )
            return s

        def __exit__(self, *args):
            pass

    with (
        patch("app.tasks.SessionLocal", side_effect=RoomMockDBSession),
        patch(
            "app.tasks.download_media_url",
            side_effect=RuntimeError("Network download error"),
        ),
        pytest.raises(RuntimeError, match="Network download error"),
    ):
        job_ingest_room_url(
            event_id=10,
            room="Room URL",
            video_url="https://example.com/recordings/broken.mp4",
            recording_start_iso=now.isoformat(),
            talk_ids=[205],
        )

    assert talk.status == "broken"
    assert len(jobs_dict) == 1
    assert jobs_dict[1].status == "failed"


def test_resolve_direct_media_url():
    from app.ingest import resolve_direct_media_url

    # Direct URL passes through
    assert (
        resolve_direct_media_url("https://example.com/recordings/room1.mp4")
        == "https://example.com/recordings/room1.mp4"
    )

    # Dropbox URL normalizes dl=1
    db_res = resolve_direct_media_url(
        "https://www.dropbox.com/s/abcdef123/video.mp4?dl=0"
    )
    assert "dl=1" in db_res

    # Google Drive share link normalizes to export endpoint
    gd_res = resolve_direct_media_url(
        "https://drive.google.com/file/d/1AbC-xYz_123/view?usp=sharing"
    )
    assert (
        gd_res
        == "https://drive.google.com/uc?export=download&id=1AbC-xYz_123&confirm=t"
    )

    # Nextcloud share link normalizes to /download
    nc_res = resolve_direct_media_url("https://cloud.example.org/s/abc123XYZ")
    assert nc_res == "https://cloud.example.org/s/abc123XYZ/download"


def test_download_media_url_streaming_success(tmp_path):
    from unittest.mock import MagicMock, patch

    from app.ingest import download_media_url

    dummy_content = b"x" * 50000
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "video/mp4"}
    mock_resp.read.side_effect = [dummy_content[:25000], dummy_content[25000:], b""]

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / "staged_download.mp4"
    with patch("urllib.request.build_opener", return_value=mock_opener):
        res = download_media_url("https://example.com/stream.mp4", target)

    assert res == target
    assert target.exists()
    assert target.read_bytes() == dummy_content


def test_download_media_url_exceeds_max_bytes(tmp_path):
    from unittest.mock import MagicMock, patch

    from app.ingest import download_media_url

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "video/mp4"}
    # Return 2MB of data when limit is patched to 1MB
    mock_resp.read.side_effect = [b"A" * 1024 * 1024, b"B" * 1024 * 1024, b""]

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / "too_large.mp4"
    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("app.config.settings.max_url_download_bytes", 1024 * 1024),
        pytest.raises(ValueError, match="Download exceeded maximum allowed size"),
    ):
        download_media_url("https://example.com/too_large.mp4", target)

    assert not target.exists()


def test_download_media_url_rejects_html_page(tmp_path):
    from unittest.mock import MagicMock, patch

    from app.ingest import download_media_url

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / "out.mp4"
    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        pytest.raises(ValueError, match="YouTube watch/stream links are web pages"),
    ):
        download_media_url("https://youtu.be/WVR7ZfuhoU8", target)


# ── Cut-Bounds Pre-Seeding Tests ────────────────────────────────────────────


def _make_detect_result(duration: float) -> DetectResult:
    return DetectResult(
        passed=True,
        actual_duration_seconds=duration,
        has_video=True,
        has_audio=True,
        reason=None,
    )


def _run_detect_with_seed(talk: Talk, recording_start) -> None:
    """Helper: run job_detect with mocked DB & storage, returning the mutated talk."""
    jobs: dict = {}
    db_ctx = MockDBContext(talk, jobs)
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", return_value=_make_detect_result(14400.0)),
    ):
        job_detect(talk.id, "1/raw/raw.mp4", recording_start)


def test_seed_cut_bounds_happy_path(dummy_talk):
    """Seeds cut_start/cut_end from schedule offsets when both are unset."""
    dummy_talk.status = "detecting"
    dummy_talk.cut_start = None
    dummy_talk.cut_end = None
    # Recording started at 09:00 UTC; talk is 09:15–09:55
    dummy_talk.start = datetime(2026, 8, 29, 9, 15, tzinfo=UTC)
    dummy_talk.end = datetime(2026, 8, 29, 9, 55, tzinfo=UTC)
    rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

    _run_detect_with_seed(dummy_talk, rec_start)

    assert dummy_talk.cut_start == pytest.approx(900.0)  # 15 min
    assert dummy_talk.cut_end == pytest.approx(3300.0)  # 55 min
    assert dummy_talk.status == "pending_approval"


def test_seed_cut_bounds_no_overwrite_when_already_set(dummy_talk):
    """Does not overwrite existing cut_start/cut_end if user already set them."""
    dummy_talk.status = "detecting"
    dummy_talk.cut_start = 42.0
    dummy_talk.cut_end = 1800.0
    dummy_talk.start = datetime(2026, 8, 29, 9, 15, tzinfo=UTC)
    dummy_talk.end = datetime(2026, 8, 29, 9, 55, tzinfo=UTC)
    rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

    _run_detect_with_seed(dummy_talk, rec_start)

    # Guard: both were set, so seeding must be skipped
    assert dummy_talk.cut_start == pytest.approx(42.0)
    assert dummy_talk.cut_end == pytest.approx(1800.0)


def test_seed_cut_bounds_no_op_without_recording_start(dummy_talk):
    """Bounds stay None when recording_start is not provided (graceful degradation)."""
    dummy_talk.status = "detecting"
    dummy_talk.cut_start = None
    dummy_talk.cut_end = None
    dummy_talk.start = datetime(2026, 8, 29, 9, 15, tzinfo=UTC)
    dummy_talk.end = datetime(2026, 8, 29, 9, 55, tzinfo=UTC)

    _run_detect_with_seed(dummy_talk, recording_start=None)

    assert dummy_talk.cut_start is None
    assert dummy_talk.cut_end is None
    assert dummy_talk.status == "pending_approval"


def test_seed_cut_bounds_talk_entirely_outside_recording(dummy_talk):
    """Skips seeding when offset_e <= offset_s (talk is before the recording start)."""
    dummy_talk.status = "detecting"
    dummy_talk.cut_start = None
    dummy_talk.cut_end = None
    # Recording started at 10:00; talk ended before that
    dummy_talk.start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
    dummy_talk.end = datetime(2026, 8, 29, 9, 30, tzinfo=UTC)
    rec_start = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)

    _run_detect_with_seed(dummy_talk, rec_start)

    # offset_s = max(0, -3600) = 0; offset_e = min(14400, -1800) → negative → clamped to ≤0
    assert dummy_talk.cut_start is None
    assert dummy_talk.cut_end is None


def test_seed_cut_bounds_clamped_to_duration(dummy_talk):
    """cut_end is clamped to raw_duration_seconds when talk extends past the recording."""
    dummy_talk.status = "detecting"
    dummy_talk.cut_start = None
    dummy_talk.cut_end = None
    # Recording started at 09:00; 4 h long (14400 s); talk goes until 13:15 → end at 15300 s > 14400
    dummy_talk.start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
    dummy_talk.end = datetime(2026, 8, 29, 13, 15, tzinfo=UTC)
    rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

    _run_detect_with_seed(dummy_talk, rec_start)

    assert dummy_talk.cut_start == pytest.approx(0.0)
    assert dummy_talk.cut_end == pytest.approx(14400.0)  # clamped to duration


def test_job_cut_fails_closed_when_newer_job_query_errors(
    dummy_talk, mock_storage, tmp_path
):
    """Worker fails closed without calling storage.put when newer cut job query fails."""
    from unittest.mock import MagicMock, patch

    from app.models import Job, Talk
    from app.tasks import job_cut

    dummy_talk.status = "cutting"
    dummy_talk.cut_start = 10.0
    dummy_talk.cut_end = 50.0
    dummy_talk.cut_segments = [{"start": 10.0, "end": 50.0}]

    mock_db = MagicMock()
    job_mock = Job(id=1, talk_id=dummy_talk.id, kind="cut", status="running")
    mock_db.get.side_effect = lambda model, oid: (
        job_mock if model == Job else dummy_talk
    )

    def add_side_effect(obj):
        if isinstance(obj, Job) and obj.id is None:
            obj.id = 1

    mock_db.add.side_effect = add_side_effect

    def query_side_effect(model):
        q = MagicMock()
        if model == Talk:
            q.filter.return_value.with_for_update.return_value.first.return_value = (
                dummy_talk
            )
            q.filter.return_value.first.return_value = dummy_talk
            return q
        if model == Job:
            raise RuntimeError("Database query failure")
        return q

    mock_db.query.side_effect = query_side_effect

    class TestDBContext:
        def __enter__(self):
            return mock_db

        def __exit__(self, *args):
            pass

    def fake_cut(inp, out, *args, **kwargs):
        Path(out).write_bytes(b"cut content")

    with (
        patch("app.tasks.SessionLocal", return_value=TestDBContext()),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue"),
        pytest.raises(RuntimeError, match="Database query failure"),
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4")

    # Storage put must never have published the cut video artifact
    cut_key = f"{dummy_talk.id}/cut/cut.mp4"
    for call in mock_storage.put.call_args_list:
        assert call[0][0] != cut_key


def test_job_cut_discards_output_when_talk_superseded_or_aborted(
    dummy_talk, mock_storage
):
    """Worker discards output without calling storage.put if talk was aborted or superseded."""
    from unittest.mock import MagicMock, patch

    from app.models import Job, Talk
    from app.tasks import job_cut

    dummy_talk.status = "cutting"
    dummy_talk.cut_start = 10.0
    dummy_talk.cut_end = 50.0

    mock_db = MagicMock()
    aborted_talk = Talk(
        id=dummy_talk.id,
        status="waiting_for_files",
        cut_start=None,
        cut_end=None,
    )
    job_mock = Job(id=1, talk_id=dummy_talk.id, kind="cut", status="running")

    def add_side_effect(obj):
        if isinstance(obj, Job) and obj.id is None:
            obj.id = 1

    mock_db.add.side_effect = add_side_effect

    def query_side_effect(model):
        q = MagicMock()
        if model == Talk:
            q.filter.return_value.with_for_update.return_value.first.return_value = (
                aborted_talk
            )
            q.filter.return_value.first.return_value = aborted_talk
            return q
        if model == Job:
            q.filter.return_value.first.return_value = None
            return q
        return q

    mock_db.query.side_effect = query_side_effect
    mock_db.get.side_effect = lambda model, oid: (
        job_mock if model == Job else dummy_talk
    )

    class TestDBContext:
        def __enter__(self):
            return mock_db

        def __exit__(self, *args):
            pass

    def fake_cut(inp, out, *args, **kwargs):
        Path(out).write_bytes(b"cut content")

    with (
        patch("app.tasks.SessionLocal", return_value=TestDBContext()),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_cut(dummy_talk.id, "1/raw/raw.mp4")

    cut_key = f"{dummy_talk.id}/cut/cut.mp4"
    for call in mock_storage.put.call_args_list:
        assert call[0][0] != cut_key
    mock_enqueue.assert_not_called()


def test_job_cut_multi_invokes_job_cut():
    """Verify job_cut_multi acts as an entrypoint delegating to job_cut."""
    from unittest.mock import patch

    from app.tasks import job_cut_multi

    with patch("app.tasks.job_cut") as mock_job_cut:
        job_cut_multi(42, "42/raw/video.mp4", "42/cut/custom.mp4")
        mock_job_cut.assert_called_once_with(
            42, "42/raw/video.mp4", "42/cut/custom.mp4"
        )
