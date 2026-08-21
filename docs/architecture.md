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

### Exemption Marker
If you have a legitimate non-media file I/O operation (e.g. reading a static application config file or writing a crash log), you must annotate it with an explicit exemption comment:

```python
# Inline exemption:
with open("config.json", "r") as f:  # storage-boundary-exempt: reading static configuration file
    config = json.load(f)

# Or preceding line exemption:
# storage-boundary-exempt: loading local environment config
data = Path("/etc/config.json").read_text()
```
