import shutil
from pathlib import Path
from unittest import mock

import pytest

from app.storage import (
    INTERMEDIATE_STAGES,
    LocalDiskBackend,
    StorageBackend,
    StorageKeyNotFoundError,
    cleanup_intermediates,
)
from tests.conftest import FakeStorageBackend


@pytest.fixture(params=["local", "fake"])
def storage_backend(request, tmp_path: Path) -> StorageBackend:
    if request.param == "local":
        return LocalDiskBackend(data_dir=tmp_path)
    return FakeStorageBackend()


def test_put_and_get_bytes(storage_backend: StorageBackend):
    key = "talk_1/raw/video.mp4"
    content = b"test content"

    storage_backend.put(key, content)

    assert storage_backend.exists(key)
    path = storage_backend.get(key)
    assert path.read_bytes() == content


def test_put_and_get_file(storage_backend: StorageBackend, tmp_path: Path):
    key = "talk_2/cut/video.mp4"
    content = b"file content"

    source_file = tmp_path / "source.mp4"
    source_file.write_bytes(content)

    storage_backend.put(key, source_file)

    assert storage_backend.exists(key)
    path = storage_backend.get(key)
    assert path.read_bytes() == content


def test_put_overwrites_silently(storage_backend: StorageBackend):
    key = "talk_1/raw/video.mp4"

    storage_backend.put(key, b"old content")
    assert storage_backend.get(key).read_bytes() == b"old content"

    storage_backend.put(key, b"new content")
    assert storage_backend.get(key).read_bytes() == b"new content"


def test_get_missing_key_raises(storage_backend: StorageBackend):
    with pytest.raises(StorageKeyNotFoundError) as exc_info:
        storage_backend.get("missing/file.mp4")
    assert exc_info.value.key == "missing/file.mp4"
    assert "missing/file.mp4" in str(exc_info.value)


def test_delete_idempotent(storage_backend: StorageBackend):
    key = "talk_1/raw/video.mp4"

    # Delete missing should not raise
    storage_backend.delete(key)

    # Put and then delete
    storage_backend.put(key, b"content")
    assert storage_backend.exists(key)

    storage_backend.delete(key)
    assert not storage_backend.exists(key)

    # Delete again should not raise
    storage_backend.delete(key)


def test_url(storage_backend: StorageBackend):
    key = "talk_1/raw/video.mp4"
    storage_backend.put(key, b"content")

    url = storage_backend.url(key)
    if isinstance(storage_backend, LocalDiskBackend):
        expected_path = storage_backend._get_path(key)
        assert url == expected_path.as_uri()
    else:
        assert url == f"memory://{key}"


def test_free_bytes(storage_backend: StorageBackend, tmp_path: Path):
    free = storage_backend.free_bytes()

    if isinstance(storage_backend, LocalDiskBackend):
        expected_free = shutil.disk_usage(tmp_path).free
        assert isinstance(free, int)
        assert free > 0
        assert abs(free - expected_free) < 1024 * 1024 * 10
    else:
        assert free == FakeStorageBackend.DEFAULT_FREE_BYTES


def test_put_interrupted_write(tmp_path: Path):
    storage_backend = LocalDiskBackend(data_dir=tmp_path)
    key = "talk_1/raw/large_video.mp4"
    target_path = storage_backend._get_path(key)

    source_file = tmp_path / "source.mp4"
    source_file.write_bytes(b"some content")

    with mock.patch(
        "app.storage.shutil.copyfileobj", side_effect=RuntimeError("Disk write failed!")
    ):
        with pytest.raises(RuntimeError, match="Disk write failed!"):
            storage_backend.put(key, source_file)

        # Ensure the final key does not exist (no partial file)
        assert not target_path.exists()
        assert len(list(target_path.parent.glob("*"))) == 0


def test_invalid_key_traversal(tmp_path: Path):
    storage_backend = LocalDiskBackend(data_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid key"):
        storage_backend._get_path("../../../etc/passwd")


def test_prefix_operations(storage_backend: StorageBackend):
    key = "talk_1/raw/video.mp4"
    prefix = "talk_1/raw"
    storage_backend.put(key, b"content")

    # Prefix exists() should return False
    assert not storage_backend.exists(prefix)

    # Prefix get() should raise StorageKeyNotFoundError
    with pytest.raises(StorageKeyNotFoundError):
        storage_backend.get(prefix)

    # Prefix delete() should remove the whole directory (local) or matching keys (fake)
    storage_backend.delete(prefix)
    assert not storage_backend.exists(key)
    if isinstance(storage_backend, LocalDiskBackend):
        assert not storage_backend._get_path(prefix).exists()


def test_fake_set_free_bytes():
    fake = FakeStorageBackend()
    assert fake.free_bytes() == FakeStorageBackend.DEFAULT_FREE_BYTES
    fake.set_free_bytes(500)
    assert fake.free_bytes() == 500


def test_list_keys(storage_backend: StorageBackend):
    storage_backend.put("talk_1/raw/video1.mp4", b"v1")
    storage_backend.put("talk_1/raw/nested/video2.mp4", b"v2")
    storage_backend.put("talk_1/cut/cut.mp4", b"cut")
    storage_backend.put("talk_2/raw/other.mp4", b"other")

    raw_keys = storage_backend.list_keys("talk_1/raw")
    assert "talk_1/raw/video1.mp4" in raw_keys
    assert "talk_1/raw/nested/video2.mp4" in raw_keys
    assert len(raw_keys) == 2

    talk_1_keys = storage_backend.list_keys("talk_1")
    assert len(talk_1_keys) == 3

    exact_key = storage_backend.list_keys("talk_1/cut/cut.mp4")
    assert exact_key == ["talk_1/cut/cut.mp4"]

    non_existent = storage_backend.list_keys("non_existent")
    assert non_existent == []


def test_list_keys_traversal(tmp_path: Path):
    backend = LocalDiskBackend(data_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid key"):
        backend.list_keys("../../../etc")


def test_cleanup_intermediates_purges_intermediates_preserves_raw_and_final(
    storage_backend: StorageBackend,
):
    talk_id = 42
    storage_backend.put(f"{talk_id}/raw/video.mp4", b"raw video")
    storage_backend.put(f"{talk_id}/final/final.mp4", b"final video")
    for stage in INTERMEDIATE_STAGES:
        storage_backend.put(f"{talk_id}/{stage}/{stage}.mp4", f"{stage} video".encode())

    cleanup_intermediates(storage_backend, talk_id)

    assert storage_backend.exists(f"{talk_id}/raw/video.mp4")
    assert storage_backend.exists(f"{talk_id}/final/final.mp4")
    for stage in INTERMEDIATE_STAGES:
        assert not storage_backend.exists(f"{talk_id}/{stage}/{stage}.mp4")


def test_cleanup_intermediates_idempotent_when_stages_not_generated(
    storage_backend: StorageBackend,
):
    talk_id = 43
    # Only raw and cut exist; preview, assemble, intro, outro were never generated
    storage_backend.put(f"{talk_id}/raw/video.mp4", b"raw video")
    storage_backend.put(f"{talk_id}/cut/cut.mp4", b"cut video")

    cleanup_intermediates(storage_backend, talk_id)

    assert storage_backend.exists(f"{talk_id}/raw/video.mp4")
    assert not storage_backend.exists(f"{talk_id}/cut/cut.mp4")


def test_cleanup_intermediates_resilient_to_storage_delete_errors():
    mock_backend = mock.MagicMock(spec=StorageBackend)
    mock_backend.delete.side_effect = RuntimeError("Storage connection failed")

    # Should not raise exception
    cleanup_intermediates(mock_backend, 42)
    assert mock_backend.delete.call_count == len(INTERMEDIATE_STAGES)
