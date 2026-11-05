"""Final pipeline stage: publishes transcoded media into storage."""

from __future__ import annotations

from pathlib import Path

from ..storage import StorageBackend


def publish(local_path: Path | str, talk_id: int, backend: StorageBackend) -> str:
    """Move transcoded media output to published storage and return its URL.

    Args:
        local_path: Local filesystem path to the transcoded output.
        talk_id: Identifier of the associated talk.
        backend: Target storage backend abstraction.

    Returns:
        The URL/URI string for the published media asset.
    """
    path = Path(local_path)
    key = f"{talk_id}/final/{path.name}"
    backend.put(key, source=path)
    return backend.url(key)
