from pathlib import Path

from app.config import settings
from app.schemas import RecordingIngestRequest
from app.storage import StorageBackend


class IngestPathRejectedError(ValueError):
    pass


def stage_recording(
    talk_id: int, payload: RecordingIngestRequest, backend: StorageBackend
) -> str:
    path_str = payload.source_path or payload.relative_key
    if "\0" in path_str:
        raise IngestPathRejectedError("Invalid path")

    target_path = Path(path_str)
    resolved_path = None

    # Resolve against roots
    roots = [Path(r).resolve() for r in settings.ingest_roots]

    if payload.source_path:
        try:
            candidate = target_path.resolve(strict=True)
            if any(candidate.is_relative_to(root) for root in roots):
                resolved_path = candidate
        except FileNotFoundError:
            pass
    else:
        for root in roots:
            try:
                candidate = (root / target_path).resolve(strict=True)
                if candidate.is_relative_to(root):
                    resolved_path = candidate
                    break
            except FileNotFoundError:
                pass

    if not resolved_path or not resolved_path.is_file():
        raise IngestPathRejectedError("Invalid or missing ingest path")

    key = f"{talk_id}/raw/{resolved_path.name}"
    backend.put(key=key, source=resolved_path)
    return key
