import os
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.config import settings
from app.ingest import IngestPathRejectedError, stage_recording
from app.schemas import RecordingIngestRequest


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
    return Mock()


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
    target.touch()

    payload = RecordingIngestRequest(source_path=str(target))
    key = stage_recording(1, payload, mock_backend)

    assert key == "1/raw/video.mp4"
    mock_backend.put.assert_called_once_with(
        key=key, source=target.resolve(strict=True)
    )


def test_happy_path_relative_key(ingest_root, mock_backend):
    target = ingest_root / "subdir" / "video.mp4"
    target.parent.mkdir()
    target.touch()

    payload = RecordingIngestRequest(relative_key="subdir/video.mp4")
    key = stage_recording(2, payload, mock_backend)

    assert key == "2/raw/video.mp4"
    mock_backend.put.assert_called_once_with(
        key=key, source=target.resolve(strict=True)
    )


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
    target.touch()

    # Valid traversal logic should resolve correctly to a file inside root
    payload_valid = RecordingIngestRequest(relative_key="subdir//..//subdir//video.mp4")
    key = stage_recording(1, payload_valid, mock_backend)
    assert key == "1/raw/video.mp4"

    # Invalid traversal outside root
    payload_invalid = RecordingIngestRequest(relative_key="subdir//..//..//secret.txt")
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload_invalid, mock_backend)


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

    payload = RecordingIngestRequest(source_path=str(evil_file))
    with pytest.raises(IngestPathRejectedError):
        stage_recording(1, payload, mock_backend)

    mock_backend.put.assert_not_called()
    settings.ingest_roots = original_roots
