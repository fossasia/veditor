import shutil
from pathlib import Path
from unittest import mock

import pytest

from app.storage import LocalDiskBackend, StorageBackend, StorageKeyNotFoundError
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
