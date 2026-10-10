import logging
import tempfile
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import av

from app.config import settings
from app.schemas import RecordingIngestRequest
from app.storage import StorageBackend

logger = logging.getLogger(__name__)


class IngestPathRejectedError(ValueError):
    pass


class InsufficientStorageError(Exception):
    def __init__(self, required_bytes: int, available_bytes: int):
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        super().__init__(
            f"Insufficient storage: required {required_bytes} bytes, but only {available_bytes} bytes available"
        )


def validate_media_file(path: Path) -> None:
    try:
        with av.open(str(path)) as container:
            has_video = any(s.type == "video" for s in container.streams)
    except av.FFmpegError as exc:
        raise IngestPathRejectedError(f"Invalid media file: {exc}") from exc

    if not has_video:
        raise IngestPathRejectedError(
            "File is not a valid video (no video stream found)"
        )


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

    matched_root = None
    if payload.source_path:
        if not target_path.is_absolute():
            raise IngestPathRejectedError("source_path must be absolute")
        try:
            candidate = target_path.resolve(strict=True)
            for root in roots:
                if candidate.is_relative_to(root):
                    resolved_path = candidate
                    matched_root = root
                    break
        except OSError, RuntimeError:
            pass
    else:
        if target_path.is_absolute():
            raise IngestPathRejectedError("relative_key must be relative")
        for root in roots:
            try:
                candidate = (root / target_path).resolve(strict=True)
                if candidate.is_relative_to(root):
                    resolved_path = candidate
                    matched_root = root
                    break
            except OSError, RuntimeError:
                pass

    if not resolved_path or not resolved_path.is_file() or not matched_root:
        raise IngestPathRejectedError("Invalid or missing ingest path")

    file_size = resolved_path.stat().st_size
    required_bytes = int(
        (
            Decimal(file_size) * Decimal(str(settings.disk_guard_multiplier))
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    available_bytes = backend.free_bytes()
    if available_bytes < required_bytes:
        raise InsufficientStorageError(
            required_bytes=required_bytes,
            available_bytes=available_bytes,
        )

    validate_media_file(resolved_path)

    rel_path = resolved_path.relative_to(matched_root)
    key = f"{talk_id}/raw/{rel_path}"
    backend.put(key=key, source=resolved_path)
    return key


def get_upload_staging_dir() -> Path:
    base = (
        Path(settings.ingest_roots[0])
        if settings.ingest_roots
        else Path(tempfile.gettempdir()) / "veditor_staging"
    ).resolve()
    # storage-boundary-exempt: upload staging directory
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_bumper_staging_dir() -> Path:
    base = get_upload_staging_dir() / "bumpers"
    # storage-boundary-exempt: bumper staging directory
    base.mkdir(parents=True, exist_ok=True)
    return base


def stage_custom_clip(
    talk_id: int, path_str: str, stage: str, backend: StorageBackend
) -> str:
    """Validate a custom clip path against ingest roots and media constraints,

    then stage it into {talk_id}/{stage}/{stage}.mp4 in storage.
    """
    if not path_str or "\0" in path_str:
        raise IngestPathRejectedError("Invalid path")

    roots = [Path(r).resolve() for r in settings.ingest_roots]
    resolved_path = None
    target_path = Path(path_str)
    if target_path.is_absolute():
        try:
            candidate = target_path.resolve(strict=True)
            for root in roots:
                if candidate.is_relative_to(root):
                    resolved_path = candidate
                    break
        except OSError, RuntimeError:
            pass
    elif (
        len(target_path.parts) == 2
        and target_path.parts[0] == "bumpers"
        and target_path.name.startswith(f"bumper_{talk_id}_{stage}_")
    ):
        bumper_root = get_bumper_staging_dir().parent.resolve()
        try:
            candidate = (bumper_root / target_path).resolve(strict=True)
            if candidate.is_relative_to(bumper_root):
                resolved_path = candidate
        except OSError, RuntimeError:
            pass
    else:
        raise IngestPathRejectedError(
            f"custom_{stage}_path must be an absolute ingest path or staging key"
        )

    if not resolved_path or not resolved_path.is_file():
        raise IngestPathRejectedError(
            f"Invalid or missing custom {stage} path: outside allowed ingest roots"
        )

    validate_media_file(resolved_path)

    key = f"{talk_id}/{stage}/{stage}.mp4"
    backend.put(key=key, source=resolved_path)
    return key


def download_media_url(url: str, output_path: Path) -> Path:
    """Download a video or stream URL using yt-dlp to a specified destination Path.

    Returns the path to the downloaded file.
    """
    import yt_dlp

    # storage-boundary-exempt: upload staging directory
    output_path.parent.mkdir(parents=True, exist_ok=True)

    outtmpl = str(output_path)
    if not outtmpl.endswith(".%(ext)s"):
        stem = output_path.stem
        parent = output_path.parent
        outtmpl = str(parent / f"{stem}.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "match_filter": yt_dlp.utils.match_filter_func("!is_live"),
        "max_filesize": getattr(settings, "max_url_download_bytes", None)
        or (20 * 1024 * 1024 * 1024),
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            downloaded = Path(filename)
            if not downloaded.exists():
                candidate_mp4 = downloaded.with_suffix(".mp4")
                if candidate_mp4.exists():
                    downloaded = candidate_mp4
            if not downloaded.exists():
                matches = list(output_path.parent.glob(f"{output_path.stem}.*"))
                if matches:
                    downloaded = matches[0]

        if not downloaded.exists():
            raise FileNotFoundError(f"Failed to find downloaded file for {url}")

        return downloaded
    except Exception:
        # Clean up any partial files matching this invocation's unique output prefix
        for partial_file in output_path.parent.glob(f"{output_path.stem}*"):
            try:
                if partial_file.is_file():
                    # storage-boundary-exempt: cleanup partial download on failure
                    partial_file.unlink(missing_ok=True)
            except OSError as cleanup_err:
                logger.warning(
                    "Failed to remove partial download file %s: %s",
                    partial_file,
                    cleanup_err,
                )
        raise
