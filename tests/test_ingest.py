import math
import os
from pathlib import Path
from unittest.mock import Mock

import av
import pytest
from pydantic import ValidationError

from app.config import settings
from app.ingest import (
    IngestPathRejectedError,
    InsufficientStorageError,
    stage_recording,
)
from app.schemas import RecordingIngestRequest


def create_test_video(path: Path) -> Path:
    container = av.open(str(path), mode="w")
    stream = container.add_stream("h264", rate=30)
    stream.width = 64
    stream.height = 64
    stream.pix_fmt = "yuv420p"
    frame = av.VideoFrame(64, 64, "yuv420p")
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


@pytest.fixture
def ingest_root(tmp_path):
    root = tmp_path / "ingest"
    root.mkdir()

    original_roots = settings.ingest_roots
    settings.ingest_roots = [root]
    yield root
    settings.ingest_roots = original_roots


@pytest.fixture
def mock_backend():
    backend = Mock()
    backend.free_bytes.return_value = 1024 * 1024 * 1024 * 100
    return backend


def test_schema_validation_exactly_one():
    with pytest.raises(ValidationError):
        RecordingIngestRequest()

    with pytest.raises(ValidationError):
        RecordingIngestRequest(source_path="/a", relative_key="b")

    # These should work
    RecordingIngestRequest(source_path="/a")
    RecordingIngestRequest(relative_key="b")


def test_happy_path_source_path(ingest_root, mock_backend):
    target = ingest_root / "video.mp4"
    create_test_video(target)

    payload = RecordingIngestRequest(source_path=str(target))
    key = stage_recording(1, payload, mock_backend)

    assert key == "1/raw/video.mp4"
    mock_backend.put.assert_called_once_with(
        key=key, source=target.resolve(strict=True)
    )


def test_happy_path_relative_key(ingest_root, mock_backend):
    target = ingest_root / "subdir" / "video.mp4"
    target.parent.mkdir()
    create_test_video(target)

    payload = RecordingIngestRequest(relative_key="subdir/video.mp4")
    key = stage_recording(2, payload, mock_backend)

    assert key == "2/raw/subdir/video.mp4"
    mock_backend.put.assert_called_once_with(
        key=key, source=target.resolve(strict=True)
    )


def test_disambiguated_storage_keys_same_basename(ingest_root, mock_backend):
    cam1 = ingest_root / "cam1" / "video.mp4"
    cam2 = ingest_root / "cam2" / "video.mp4"
    cam1.parent.mkdir()
    cam2.parent.mkdir()
    create_test_video(cam1)
    create_test_video(cam2)

    key1 = stage_recording(
        1, RecordingIngestRequest(relative_key="cam1/video.mp4"), mock_backend
    )
    key2 = stage_recording(
        1, RecordingIngestRequest(relative_key="cam2/video.mp4"), mock_backend
    )

    assert key1 == "1/raw/cam1/video.mp4"
    assert key2 == "1/raw/cam2/video.mp4"
    assert key1 != key2


def test_adversarial_relative_traversal(ingest_root, mock_backend):
    outside_file = ingest_root.parent / "secret.txt"
    outside_file.touch()

    payload = RecordingIngestRequest(relative_key="../secret.txt")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    mock_backend.put.assert_not_called()


def test_adversarial_absolute_outside(ingest_root, mock_backend):
    outside_file = ingest_root.parent / "secret.txt"
    if not outside_file.exists():
        outside_file.touch()

    payload = RecordingIngestRequest(source_path=str(outside_file))
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    mock_backend.put.assert_not_called()


def test_adversarial_symlink_to_outside(ingest_root, mock_backend):
    outside_file = ingest_root.parent / "secret.txt"
    if not outside_file.exists():
        outside_file.touch()

    symlink_path = ingest_root / "link.mp4"
    os.symlink(outside_file, symlink_path)

    # Try via source_path
    payload1 = RecordingIngestRequest(source_path=str(symlink_path))
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload1, mock_backend)

    # Try via relative_key
    payload2 = RecordingIngestRequest(relative_key="link.mp4")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload2, mock_backend)

    mock_backend.put.assert_not_called()


def test_adversarial_chained_symlinks(ingest_root, mock_backend):
    outside_file = ingest_root.parent / "secret.txt"
    if not outside_file.exists():
        outside_file.touch()

    link1 = ingest_root / "link1"
    os.symlink(outside_file, link1)

    link2 = ingest_root / "link2.mp4"
    os.symlink(link1, link2)

    payload = RecordingIngestRequest(source_path=str(link2))
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    mock_backend.put.assert_not_called()


def test_adversarial_trailing_slash_double_slash(ingest_root, mock_backend):
    target = ingest_root / "subdir" / "video.mp4"
    target.parent.mkdir(exist_ok=True)
    create_test_video(target)

    # Valid traversal logic should resolve correctly to a file inside root
    payload_valid = RecordingIngestRequest(relative_key="subdir//..//subdir//video.mp4")
    key = stage_recording(1, payload_valid, mock_backend)
    assert key == "1/raw/subdir/video.mp4"
    assert mock_backend.put.call_count == 1

    # Invalid traversal outside root
    payload_invalid = RecordingIngestRequest(relative_key="subdir//..//..//secret.txt")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload_invalid, mock_backend)
    assert mock_backend.put.call_count == 1


def test_adversarial_url_encoded_traversal(ingest_root, mock_backend):
    # pathlib doesn't automatically url-decode, so %2e%2e%2f will be treated as literal filename.
    # If the file doesn't exist, it rejects.
    # Just verify that injecting it doesn't bypass checks.
    payload = RecordingIngestRequest(relative_key="%2e%2e%2fsecret.txt")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)
    mock_backend.put.assert_not_called()


def test_adversarial_null_byte_injection(ingest_root, mock_backend):
    payload = RecordingIngestRequest(relative_key="video.mp4\0")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)
    mock_backend.put.assert_not_called()


def test_adversarial_prefix_collision(tmp_path, mock_backend):
    ingest_root = tmp_path / "ingest"
    ingest_root.mkdir()

    evil_root = tmp_path / "ingest-evil"
    evil_root.mkdir()
    evil_file = evil_root / "video.mp4"
    evil_file.touch()

    original_roots = settings.ingest_roots
    settings.ingest_roots = [ingest_root]

    try:
        payload = RecordingIngestRequest(source_path=str(evil_file))
        with pytest.raises(IngestPathRejectedError):
            stage_recording(1, payload, mock_backend)

        mock_backend.put.assert_not_called()
    finally:
        settings.ingest_roots = original_roots


def test_source_path_rejects_relative_path(ingest_root, mock_backend):
    payload = RecordingIngestRequest(source_path="relative/path.mp4")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)
    mock_backend.put.assert_not_called()


def test_relative_key_rejects_absolute_path(ingest_root, mock_backend):
    payload = RecordingIngestRequest(relative_key="/absolute/path.mp4")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)
    mock_backend.put.assert_not_called()


def test_empty_ingest_roots_rejects_all(tmp_path, mock_backend):
    original_roots = settings.ingest_roots
    settings.ingest_roots = []

    target = tmp_path / "video.mp4"
    target.touch()

    try:
        payload = RecordingIngestRequest(source_path=str(target))
        with pytest.raises(IngestPathRejectedError):
            stage_recording(1, payload, mock_backend)

        payload2 = RecordingIngestRequest(relative_key="video.mp4")
        with pytest.raises(IngestPathRejectedError):
            stage_recording(1, payload2, mock_backend)
        mock_backend.put.assert_not_called()
    finally:
        settings.ingest_roots = original_roots


def test_ingest_directory_rejected(ingest_root, mock_backend):
    target_dir = ingest_root / "subdir"
    target_dir.mkdir(exist_ok=True)

    payload = RecordingIngestRequest(source_path=str(target_dir))
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    payload2 = RecordingIngestRequest(relative_key="subdir")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload2, mock_backend)
    mock_backend.put.assert_not_called()


def test_settings_ingest_roots_validation(tmp_path):
    from app.config import Settings

    abs_path = tmp_path / "recordings"
    cfg = Settings(ingest_roots=[abs_path])
    assert cfg.ingest_roots == [abs_path.resolve()]

    with pytest.raises(ValidationError):
        Settings(ingest_roots=["relative/recordings"])


def test_non_media_file_rejected(ingest_root, mock_backend):
    text_file = ingest_root / "document.txt"
    text_file.write_text("Hello, World!")

    payload = RecordingIngestRequest(relative_key="document.txt")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    mock_backend.put.assert_not_called()


def test_audio_only_file_rejected(ingest_root, mock_backend):
    audio_file = ingest_root / "audio.mp3"
    container = av.open(str(audio_file), mode="w")
    container.add_stream("mp3")
    container.close()

    payload = RecordingIngestRequest(relative_key="audio.mp3")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    mock_backend.put.assert_not_called()


def test_stage_recording_insufficient_storage_rejected(ingest_root, mock_backend):
    target = ingest_root / "video.mp4"
    create_test_video(target)
    file_size = target.stat().st_size
    required_bytes = math.ceil(file_size * settings.disk_guard_multiplier)

    # Set free bytes to less than required
    mock_backend.free_bytes.return_value = required_bytes - 1

    payload = RecordingIngestRequest(relative_key="video.mp4")
    with pytest.raises(InsufficientStorageError) as exc_info:
        stage_recording(1, payload, mock_backend)

    err = exc_info.value
    assert err.required_bytes == required_bytes
    assert err.available_bytes == required_bytes - 1
    assert str(required_bytes) in str(err)
    assert str(required_bytes - 1) in str(err)
    mock_backend.put.assert_not_called()


def test_stage_recording_custom_disk_guard_multiplier(ingest_root, mock_backend):
    target = ingest_root / "video.mp4"
    create_test_video(target)
    file_size = target.stat().st_size

    original_multiplier = settings.disk_guard_multiplier
    try:
        settings.disk_guard_multiplier = 5.0
        required_bytes = math.ceil(file_size * 5.0)

        # 4x is enough for default (3x) but not for 5x
        mock_backend.free_bytes.return_value = math.ceil(file_size * 4.0)

        payload = RecordingIngestRequest(relative_key="video.mp4")
        with pytest.raises(InsufficientStorageError) as exc_info:
            stage_recording(1, payload, mock_backend)

        assert exc_info.value.required_bytes == required_bytes
        mock_backend.put.assert_not_called()

        # Exactly 5x is sufficient
        mock_backend.free_bytes.return_value = required_bytes
        key = stage_recording(1, payload, mock_backend)
        assert key == "1/raw/video.mp4"
        mock_backend.put.assert_called_once()
    finally:
        settings.disk_guard_multiplier = original_multiplier


def test_disk_guard_multiplier_validation():
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(disk_guard_multiplier=-1.0)

    with pytest.raises(ValidationError):
        Settings(disk_guard_multiplier=0.0)

    with pytest.raises(ValidationError):
        Settings(disk_guard_multiplier=float("nan"))

    with pytest.raises(ValidationError):
        Settings(disk_guard_multiplier=float("inf"))

    s = Settings(disk_guard_multiplier=2.5)
    assert s.disk_guard_multiplier == 2.5
