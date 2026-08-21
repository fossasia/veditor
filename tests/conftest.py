import logging
from pathlib import Path

import pytest

from app.storage import StorageBackend, StorageKeyNotFoundError

logger = logging.getLogger(__name__)


class FakePath:
    """A minimal mock for pathlib.Path that provides read_bytes for tests."""

    def __init__(self, key: str, data: bytes):
        self.key = key
        self._data = data

    def read_bytes(self) -> bytes:
        return self._data

    def is_file(self) -> bool:
        return True

    def is_dir(self) -> bool:
        return False

    def as_uri(self) -> str:
        return f"memory://{self.key}"

    def exists(self) -> bool:
        return True


class FakeStorageBackend(StorageBackend):
    """
    In-memory storage backend for fast, deterministic testing.

    LIMITATION: get() returns a FakePath object that supports read_bytes() but
    cannot be opened by C libraries (like PyAV). If a pipeline test requires
    actual media decode/encode, use a LocalDiskBackend backed by a tmp_path
    instead. This fake is intended for logic around the pipeline (ingest,
    state transitions, retention).
    """

    DEFAULT_FREE_BYTES = 1024 * 1024 * 1024 * 100  # 100GB default

    def __init__(self):
        self.storage: dict[str, bytes] = {}
        self._free_bytes: int = self.DEFAULT_FREE_BYTES

    def set_free_bytes(self, size: int) -> None:
        """Helper to simulate low disk space conditions in tests."""
        self._free_bytes = size

    def put(self, key: str, source: Path | bytes) -> None:
        if isinstance(source, bytes):
            self.storage[key] = source
        else:
            self.storage[key] = Path(source).read_bytes()

    def get(self, key: str) -> Path:
        if key not in self.storage:
            raise StorageKeyNotFoundError(key)
        return FakePath(key, self.storage[key])  # type: ignore

    def url(self, key: str) -> str:
        return f"memory://{key}"

    def delete(self, key: str) -> None:
        # Idempotent delete of exact match
        self.storage.pop(key, None)
        # Delete prefixes (e.g. 'talk_1/raw')
        prefix = key if key.endswith("/") else f"{key}/"
        keys_to_delete = [k for k in self.storage if k.startswith(prefix)]
        for k in keys_to_delete:
            del self.storage[k]

    def exists(self, key: str) -> bool:
        return key in self.storage

    def free_bytes(self) -> int:
        return self._free_bytes


@pytest.fixture
def fake_storage() -> FakeStorageBackend:
    return FakeStorageBackend()


def override_storage_backend(app, fake_backend: FakeStorageBackend):
    """
    Helper to override the FastAPI dependency for route-level tests.
    Usage:
        override_storage_backend(app, fake_storage)
    """
    try:
        from app.routes.ops import get_storage_backend

        app.dependency_overrides[get_storage_backend] = lambda: fake_backend
    except ImportError as e:
        logger.warning(
            "Could not override get_storage_backend. It may not be implemented yet. Error: %s",
            e,
        )
