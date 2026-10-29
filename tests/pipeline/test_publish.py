import ast
from pathlib import Path

import pytest

from app.pipeline.publish import publish
from app.storage import LocalDiskBackend
from tests.conftest import FakeStorageBackend, generate_clip
from tests.test_pipeline_imports import is_forbidden
from tests.test_storage_boundary import find_storage_violations


def test_publish_stores_at_expected_key_and_returns_url(
    tmp_path: Path, fake_storage: FakeStorageBackend
) -> None:
    clip = generate_clip(1.0, output_dir=tmp_path)
    expected_key = f"42/final/{clip.name}"

    url = publish(clip, talk_id=42, backend=fake_storage)

    assert url == fake_storage.url(expected_key)
    assert fake_storage.exists(expected_key)
    stored_path = fake_storage.get(expected_key)
    assert stored_path.read_bytes() == clip.read_bytes()


def test_publish_overwrites_cleanly(
    tmp_path: Path, fake_storage: FakeStorageBackend
) -> None:
    v1_dir = tmp_path / "v1"
    v2_dir = tmp_path / "v2"
    v1_dir.mkdir()
    v2_dir.mkdir()

    clip_v1 = generate_clip(1.0, output_dir=v1_dir)
    filename = clip_v1.name
    expected_key = f"42/final/{filename}"

    publish(clip_v1, talk_id=42, backend=fake_storage)
    assert fake_storage.get(expected_key).read_bytes() == clip_v1.read_bytes()

    # Create distinct version 2 with identical filename
    clip_v2_temp = generate_clip(2.0, output_dir=v2_dir)
    clip_v2 = v2_dir / filename
    clip_v2_temp.rename(clip_v2)

    url_v2 = publish(clip_v2, talk_id=42, backend=fake_storage)

    assert url_v2 == fake_storage.url(expected_key)
    assert fake_storage.get(expected_key).read_bytes() == clip_v2.read_bytes()


def test_publish_with_local_disk_backend(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    backend = LocalDiskBackend(storage_root)
    clip = generate_clip(1.0, output_dir=tmp_path / "source")
    expected_key = f"99/final/{clip.name}"

    url = publish(clip, talk_id=99, backend=backend)

    assert backend.exists(expected_key)
    assert url == backend.url(expected_key)
    assert url.startswith("file://")
    stored_file = backend.get(expected_key)
    assert stored_file.read_bytes() == clip.read_bytes()


def test_publish_storage_boundary_isolated() -> None:
    publish_path = (
        Path(__file__).parent.parent.parent / "app" / "pipeline" / "publish.py"
    )
    source_code = publish_path.read_text(encoding="utf-8")
    violations = find_storage_violations(source_code, file_path=str(publish_path.name))

    assert violations == [], (
        f"Storage boundary violations found in publish.py: {violations}"
    )


def test_publish_pipeline_imports_isolated() -> None:
    publish_path = (
        Path(__file__).parent.parent.parent / "app" / "pipeline" / "publish.py"
    )
    source_code = publish_path.read_text(encoding="utf-8")
    tree = ast.parse(source_code)

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_forbidden(alias.name):
                    violations.append(
                        f"Direct import of forbidden module: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if node.level >= 2:
                module_name = f"app.{module_name}" if module_name else "app"
            for alias in node.names:
                full_name = f"{module_name}.{alias.name}" if module_name else alias.name
                if is_forbidden(module_name) or is_forbidden(full_name):
                    violations.append(
                        f"Import of forbidden module: {full_name} from {node.module}"
                    )

    assert violations == [], f"Forbidden imports found in publish.py: {violations}"


def test_publish_nonexistent_file_raises_error(
    tmp_path: Path, fake_storage: FakeStorageBackend
) -> None:
    missing_clip = tmp_path / "nonexistent.mp4"
    with pytest.raises(FileNotFoundError):
        publish(missing_clip, talk_id=42, backend=fake_storage)


def test_publish_accepts_str_path(
    tmp_path: Path, fake_storage: FakeStorageBackend
) -> None:
    clip = generate_clip(1.0, output_dir=tmp_path)
    expected_key = f"42/final/{clip.name}"

    url = publish(str(clip), talk_id=42, backend=fake_storage)

    assert url == fake_storage.url(expected_key)
    assert fake_storage.exists(expected_key)
