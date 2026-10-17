import http.cookiejar
import logging
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import ROUND_CEILING, Decimal
from html.parser import HTMLParser
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


def resolve_direct_media_url(url: str) -> str:
    """Normalize common cloud storage share URLs (Dropbox, Google Drive, Nextcloud)

    to direct download streams. Direct media links are returned unchanged.
    """
    clean_url = url.strip()
    parsed = urllib.parse.urlsplit(clean_url)
    host = parsed.netloc.lower()

    # Dropbox share links: ensure dl=1
    if "dropbox.com" in host:
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        qs["dl"] = ["1"]
        new_query = urllib.parse.urlencode(qs, doseq=True)
        return urllib.parse.urlunsplit(parsed._replace(query=new_query))

    # Google Drive share links: convert to export/download endpoint
    if "drive.google.com" in host:
        file_id = None
        match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
        if match:
            file_id = match.group(1)
        elif "id" in urllib.parse.parse_qs(parsed.query):
            file_id = urllib.parse.parse_qs(parsed.query)["id"][0]

        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"

    # Nextcloud / ownCloud share links (e.g. /s/<token> -> /s/<token>/download)
    if re.search(r"/s/[a-zA-Z0-9_-]+/?$", parsed.path):
        clean_path = parsed.path.rstrip("/") + "/download"
        return urllib.parse.urlunsplit(parsed._replace(path=clean_path))

    return clean_url


class _DriveFormParser(HTMLParser):
    """Extract confirmation form action and hidden parameters from Google Drive virus scan warning pages."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}
        self.in_form = False
        self.download_link: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: v or "" for k, v in attrs}
        if tag == "form":
            action = attr_dict.get("action", "")
            form_id = attr_dict.get("id", "")
            if "download" in action or form_id == "download-form":
                self.action = action
                self.in_form = True
        elif tag == "input" and self.in_form:
            name = attr_dict.get("name")
            val = attr_dict.get("value", "")
            if name:
                self.inputs[name] = val
        elif tag == "a":
            href = attr_dict.get("href", "")
            if "export=download" in href or "confirm=" in href:
                self.download_link = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_form = False


def download_media_url(url: str, output_path: Path) -> Path:
    """Download a video file directly from an HTTP/HTTPS URL into output_path.

    Streams the file chunk-by-chunk using the Python standard library with zero
    external dependencies. Automatically resolves common cloud storage share URLs
    (Dropbox, Google Drive, Nextcloud) to direct streams and enforces timeout,
    content validation, and maximum file size boundaries.

    Returns the path to the downloaded file.
    """
    resolved_url = resolve_direct_media_url(url)
    # storage-boundary-exempt: upload staging directory
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_target = output_path.with_suffix(
        output_path.suffix + f".part_{uuid.uuid4().hex[:8]}"
    )
    max_bytes = getattr(settings, "max_url_download_bytes", 20 * 1024 * 1024 * 1024)

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    req = urllib.request.Request(resolved_url, headers=headers)

    try:
        resp = opener.open(req, timeout=120)

        # Handle Google Drive large-file virus scan confirmation page
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            resp_url = resp.geturl().lower()
            if (
                "drive.google.com" in resolved_url
                or "drive.google.com" in resp_url
                or "drive.usercontent.google.com" in resp_url
            ):
                body = resp.read().decode("utf-8", errors="ignore")
                parser = _DriveFormParser()
                parser.feed(body)
                confirm_url = None
                if parser.action:
                    query = urllib.parse.urlencode(parser.inputs)
                    confirm_url = f"{parser.action}?{query}" if query else parser.action
                elif parser.download_link:
                    confirm_url = urllib.parse.urljoin(
                        "https://drive.google.com", parser.download_link
                    )

                if confirm_url:
                    req_confirm = urllib.request.Request(confirm_url, headers=headers)
                    resp = opener.open(req_confirm, timeout=120)
                else:
                    raise ValueError(
                        "Google Drive returned an HTML page without a valid download confirmation action."
                    )
            else:
                parsed_netloc = urllib.parse.urlsplit(url).netloc.lower()
                if "youtube.com" in parsed_netloc or "youtu.be" in parsed_netloc:
                    raise ValueError(
                        "YouTube watch/stream links are web pages, not direct video files. "
                        "Please provide a direct video URL (.mp4), Google Drive, or Dropbox link."
                    )
                raise ValueError(
                    f"The provided URL returned a web page ({content_type}) instead of a direct video stream. "
                    "Please ensure the link points directly to a video file, Google Drive, or Dropbox."
                )

        downloaded_bytes = 0
        chunk_size = 64 * 1024  # 64 KB chunk

        # storage-boundary-exempt: write downloaded media stream to staging file
        with open(temp_target, "wb") as f_out:
            first_chunk = True
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                if first_chunk:
                    first_chunk = False
                    stripped = chunk.lstrip()
                    if stripped.startswith(
                        (b"<!DOCTYPE", b"<html", b"<HTML", b"<?xml")
                    ):
                        raise ValueError(
                            "URL response contains HTML web markup rather than a video file. "
                            "Please provide a direct media file URL, Google Drive, or Dropbox link."
                        )
                f_out.write(chunk)
                downloaded_bytes += len(chunk)
                if downloaded_bytes > max_bytes:
                    raise ValueError(
                        f"Download exceeded maximum allowed size of {max_bytes} bytes"
                    )

        if downloaded_bytes == 0:
            raise ValueError(f"Downloaded media from '{url}' is empty (0 bytes)")

        # Atomically replace destination path
        temp_target.replace(output_path)
        return output_path

    except Exception:
        # Clean up partial download files
        # storage-boundary-exempt: cleanup partial download on failure
        temp_target.unlink(missing_ok=True)
        # storage-boundary-exempt: cleanup target output on failure
        output_path.unlink(missing_ok=True)
        raise
