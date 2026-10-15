# Architecture Guidelines

## Storage Boundary Invariant

All media and persistent file I/O operations must strictly route through `app/storage.py` via the `StorageBackend` abstraction (e.g. `LocalDiskBackend` or future `S3Backend`).

### Why this rule exists:
1. **Multi-backend portability**: Isolates storage mechanisms so switching between local filesystem and S3/MinIO requires zero changes to pipeline or business logic.
2. **Lifecycle & Retention Enforcement**: Video files (raw, cut, preview, final) follow strict retention policies. Direct file operations bypass key conventions and lead to orphaned files or failed cleanup cycles.
3. **Atomic Operations**: `storage.py` guarantees atomic file writes, preventing corrupted or half-written video assets.

### Automated Invariant Check
An AST-based architecture test in `tests/test_storage_boundary.py` automatically scans all files in `app/` (excluding `app/storage.py`) during test runs and CI. It prevents direct calls to:
- Builtin `open()` or `io.open()`
- Direct `shutil` operations (`copy`, `move`, `rmtree`, etc.)
- Direct `os` file operations (`remove`, `unlink`, `rename`, `mkdir`, etc.)
- Direct `pathlib.Path` I/O methods (`read_bytes`, `write_text`, `unlink`, etc.)

### Handling Local Paths in Pipeline and Ingest Modules

When working with media and external files across `app/pipeline/` and `app/ingest.py`:

1. **Managed Storage Assets**:
   - Always route reads through `StorageBackend.get(key)`. This returns a local readable `Path` that can be passed directly to subprocesses (`ffmpeg`, `ffprobe`) or libraries (`PyAV`).
   - Persist generated outputs by calling `StorageBackend.put(key, temp_path)`.

2. **Intentional External / Raw File Reads**:
   - For modules that intentionally access external staging directories or perform direct filesystem inspections on unmanaged source paths (e.g. `app/ingest.py` validating staged files before ingest, or `app/pipeline/detect.py` reading external schedule files or raw camera dumps), annotate direct I/O calls with `# storage-boundary-exempt: <reason>`.

### Exemption Marker
If you have a legitimate non-media file I/O operation (e.g. reading a static application config file or writing a crash log) or intentional direct path inspection in ingest/pipeline scripts, annotate the statement with an explicit exemption comment:

```python
# Inline exemption:
with open(
    "config.json", "r"
) as f:  # storage-boundary-exempt: reading static configuration file
    config = json.load(f)

# Or preceding line exemption:
# storage-boundary-exempt: inspecting external recording file before staging
metadata = raw_path.read_bytes()
```
