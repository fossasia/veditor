import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class StorageKeyNotFoundError(FileNotFoundError):
    """Raised when a requested key is not found in the storage backend."""

    def __init__(self, key: str):
        super().__init__(f"Storage key not found: {key}")
        self.key = key


class StorageBackend(Protocol):
    """
    Storage backend for managing files.
    Keys should follow the convention: {talk_id}/{stage}/{filename}
    """

    def put(self, key: str, source: Path | bytes) -> None:
        """
        Store a file at the given key.
        If the key already exists, it is overwritten silently.
        """
        ...

    def get(self, key: str) -> Path:
        """
        Retrieve a file by key, returning a local readable Path.
        Raises StorageKeyNotFoundError if the key does not exist or is a prefix.
        """
        ...

    def url(self, key: str) -> str:
        """
        Return a URI or URL for the key.
        """
        ...

    def delete(self, key: str) -> None:
        """
        Delete a file or prefix by key.
        This operation is idempotent; deleting a missing key is a no-op.
        """
        ...

    def exists(self, key: str) -> bool:
        """
        Check if a file exists at the given key. Returns False for prefixes.
        """
        ...

    def list_keys(self, prefix: str = "") -> list[str]:
        """
        List all file keys matching the given prefix.
        """
        ...

    def free_bytes(self) -> int:
        """
        Return the available free space in bytes.
        """
        ...


class LocalDiskBackend(StorageBackend):
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir).resolve()

    def _get_path(self, key: str) -> Path:
        """Resolve a key to its absolute path within the data directory."""
        # Prevent directory traversal attacks by ensuring the resolved path stays within
        # data_dir.
        path = (self.data_dir / key).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError(f"Invalid key: {key}")
        return path

    def put(self, key: str, source: Path | bytes) -> None:
        """
        Store a file at the given key via atomic write.
        Intermediate directories are created automatically.
        """
        target_path = self._get_path(key)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temporary file in the same directory, then rename atomically
        with tempfile.NamedTemporaryFile(delete=False, dir=target_path.parent) as tmp:
            try:
                if isinstance(source, bytes):
                    tmp.write(source)
                else:
                    with open(source, "rb") as f_in:
                        shutil.copyfileobj(f_in, tmp)
            except Exception:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise

        try:
            os.replace(tmp.name, target_path)
        except Exception:
            Path(tmp.name).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> Path:
        """
        Retrieve a file by key, returning a local readable Path.
        Raises StorageKeyNotFoundError if the key does not exist or is a prefix.
        """
        path = self._get_path(key)
        if not path.is_file():
            raise StorageKeyNotFoundError(key)
        return path

    def url(self, key: str) -> str:
        """
        Return a file:// URI for the key.
        """
        path = self._get_path(key)
        return path.as_uri()

    def delete(self, key: str) -> None:
        """
        Delete a file or prefix by key. Idempotent.
        """
        path = self._get_path(key)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        """
        Check if a file exists at the given key. Returns False for prefixes.
        """
        path = self._get_path(key)
        return path.is_file()

    def list_keys(self, prefix: str = "") -> list[str]:
        """
        List all file keys matching the given prefix.
        """
        path = self._get_path(prefix)
        if not path.exists():
            return []
        if path.is_file():
            return [path.relative_to(self.data_dir).as_posix()]
        keys = []
        for p in sorted(path.rglob("*")):
            if p.is_file():
                keys.append(p.relative_to(self.data_dir).as_posix())
        return keys

    def free_bytes(self) -> int:
        """
        Return the available free space in bytes.
        """
        # Ensure the data directory exists so we can get its usage
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.data_dir).free


def get_storage_backend() -> StorageBackend:
    """FastAPI dependency returning the configured StorageBackend."""
    from app.config import settings

    return LocalDiskBackend(settings.data_dir)


INTERMEDIATE_STAGES: tuple[str, ...] = (
    "cut",
    "preview",
    "assemble",
    "intro",
    "outro",
)


def cleanup_intermediates(storage: StorageBackend, talk_id: int) -> None:
    """Delete intermediate artifacts (cut, preview, assemble, intro, outro) for a talk.

    Safe and idempotent. Deletion failures are logged as warnings and not raised,
    ensuring cleanup never blocks or rolls back state transitions. raw/ is never touched.
    """
    for target in INTERMEDIATE_STAGES:
        try:
            storage.delete(f"{talk_id}/{target}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to delete %s storage for talk %s: %s",
                target,
                talk_id,
                exc,
            )
