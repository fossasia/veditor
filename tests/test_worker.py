import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import redis.exceptions

from scripts.run_worker import main


def test_eager_import_tasks_rationale() -> None:
    """Verify that scripts/run_worker.py imports app.tasks inside worker boot

    rather than at module top-level to prevent pre-fork socket leaks.
    """
    script_path = Path(__file__).parent.parent / "scripts" / "run_worker.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))

    # Verify no top-level app.tasks import (prevents parent process socket leaks)
    top_level_imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                top_level_imports.append(
                    f"{module}.{alias.name}" if module else alias.name
                )

    assert not any(
        imp == "app.tasks" or imp.startswith("app.tasks.") for imp in top_level_imports
    ), "scripts/run_worker.py must not import app.tasks at module top-level"

    # Verify app.tasks is imported inside the file (e.g. inside _run_single_worker)
    all_imports = []
    for walk_node in ast.walk(tree):
        if isinstance(walk_node, ast.Import):
            for alias in walk_node.names:
                all_imports.append(alias.name)
        elif isinstance(walk_node, ast.ImportFrom):
            module = walk_node.module or ""
            for alias in walk_node.names:
                all_imports.append(f"{module}.{alias.name}" if module else alias.name)

    assert any(
        imp == "app.tasks" or imp.startswith("app.tasks.") for imp in all_imports
    ), "scripts/run_worker.py must import app.tasks in worker process boot"


@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.Worker")
def test_worker_default_queues(
    mock_worker_cls: MagicMock, mock_redis_from_url: MagicMock
) -> None:
    mock_redis = MagicMock()
    mock_redis_from_url.return_value = mock_redis
    mock_worker = MagicMock()
    mock_worker_cls.return_value = mock_worker

    main([])

    mock_redis.ping.assert_called_once()
    mock_worker_cls.assert_called_once_with(
        ["light", "heavy"], connection=mock_redis, name=None
    )
    mock_worker.work.assert_called_once_with(burst=False)


@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.Worker")
def test_worker_explicit_queues(
    mock_worker_cls: MagicMock, mock_redis_from_url: MagicMock
) -> None:
    mock_redis = MagicMock()
    mock_redis_from_url.return_value = mock_redis
    mock_worker = MagicMock()
    mock_worker_cls.return_value = mock_worker

    main(["light"])
    mock_worker_cls.assert_called_with(["light"], connection=mock_redis, name=None)

    main(["heavy"])
    mock_worker_cls.assert_called_with(["heavy"], connection=mock_redis, name=None)

    main(["light", "heavy", "custom_queue"])
    mock_worker_cls.assert_called_with(
        ["light", "heavy", "custom_queue"], connection=mock_redis, name=None
    )


@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.Worker")
def test_worker_burst_and_name_flags(
    mock_worker_cls: MagicMock, mock_redis_from_url: MagicMock
) -> None:
    mock_redis = MagicMock()
    mock_redis_from_url.return_value = mock_redis
    mock_worker = MagicMock()
    mock_worker_cls.return_value = mock_worker

    main(["light", "--burst", "--name", "test-worker-1"])
    mock_worker_cls.assert_called_once_with(
        ["light"], connection=mock_redis, name="test-worker-1"
    )
    mock_worker.work.assert_called_once_with(burst=True)


@patch("scripts.run_worker.register_periodic_retention_sweep")
@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.Worker")
def test_worker_with_scheduler_flag(
    mock_worker_cls: MagicMock,
    mock_redis_from_url: MagicMock,
    mock_register_sweep: MagicMock,
) -> None:
    mock_redis = MagicMock()
    mock_redis_from_url.return_value = mock_redis
    mock_worker = MagicMock()
    mock_worker_cls.return_value = mock_worker

    main(["light", "--with-scheduler"])
    mock_worker_cls.assert_called_once_with(["light"], connection=mock_redis, name=None)
    mock_register_sweep.assert_called_once()
    mock_worker.work.assert_called_once_with(burst=False, with_scheduler=True)


@patch("scripts.run_worker.redis.from_url")
def test_worker_redis_connection_error_fast_fail(
    mock_redis_from_url: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_redis = MagicMock()
    mock_redis.ping.side_effect = redis.exceptions.ConnectionError(
        "Connection refused by Redis server"
    )
    mock_redis_from_url.return_value = mock_redis

    with pytest.raises(SystemExit) as exc_info:
        main(["light"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Could not connect to Redis" in captured.err, (
        f"Expected clean error on stderr, got: {captured.err}"
    )
    assert "Connection refused" in captured.err


def test_worker_redis_url_unset_fast_fail(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("scripts.run_worker.settings.redis_url", ""):
        with pytest.raises(SystemExit) as exc_info:
            main(["light"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error: REDIS_URL is unset." in captured.err


def test_worker_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_worker.py", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Run an RQ worker for VEditor" in result.stdout
    assert "queues" in result.stdout


@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.multiprocessing.get_context")
def test_worker_concurrency_spawns_processes(
    mock_get_context: MagicMock, mock_redis_from_url: MagicMock
) -> None:
    mock_redis = MagicMock()
    mock_redis_from_url.return_value = mock_redis
    mock_ctx = MagicMock()
    mock_get_context.return_value = mock_ctx
    mock_proc = MagicMock()
    mock_ctx.Process.return_value = mock_proc

    main(["light", "--concurrency", "3", "--name", "worker-light"])

    mock_redis.ping.assert_called_once()
    mock_redis.close.assert_called_once()
    mock_get_context.assert_called_once_with("spawn")
    assert mock_ctx.Process.call_count == 3
    assert mock_proc.start.call_count == 3
    assert mock_proc.join.call_count == 3


@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.Worker")
@patch("app.db.engine.dispose")
def test_run_single_worker(
    mock_dispose: MagicMock, mock_worker_cls: MagicMock, mock_redis_from_url: MagicMock
) -> None:
    from scripts.run_worker import _run_single_worker

    mock_redis = MagicMock()
    mock_redis_from_url.return_value = mock_redis
    mock_worker = MagicMock()
    mock_worker_cls.return_value = mock_worker

    _run_single_worker(["light"], "redis://localhost:6379/0", "test-w", burst=True)

    mock_dispose.assert_called_once_with(close=False)
    mock_redis_from_url.assert_called_once_with("redis://localhost:6379/0")
    mock_worker_cls.assert_called_once_with(
        ["light"], connection=mock_redis, name="test-w"
    )
    mock_worker.work.assert_called_once_with(burst=True)


@patch("os.nice")
@patch("scripts.run_worker.redis.from_url")
@patch("scripts.run_worker.Worker")
@patch("app.db.engine.dispose")
def test_worker_nice_flag(
    mock_dispose: MagicMock,
    mock_worker_cls: MagicMock,
    mock_redis_from_url: MagicMock,
    mock_os_nice: MagicMock,
) -> None:
    main(["light", "--nice", "10", "--burst"])
    mock_os_nice.assert_called_once_with(10)
    mock_worker = mock_worker_cls.return_value
    mock_worker.work.assert_called_once_with(burst=True)


def test_run_single_worker_shows_app_info_logs() -> None:
    """INFO logs from app modules, such as the dev-mode verification link, must
    appear in the worker output (#315)."""
    code = """
import logging
from unittest.mock import MagicMock, patch

with (
    patch("scripts.run_worker.redis.from_url"),
    patch("scripts.run_worker.Worker", return_value=MagicMock()),
    patch("app.db.engine.dispose"),
):
    from scripts.run_worker import _run_single_worker

    _run_single_worker(["light"], "redis://localhost:6379/0", None, burst=True)

logging.getLogger("app.email").info("verify link: http://localhost/verify-email")
# Library loggers keep their defaults; SQLAlchemy at INFO would log bound
# parameters such as email addresses.
assert not logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.INFO)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "verify link: http://localhost/verify-email" in result.stderr


def test_run_single_worker_does_not_duplicate_app_logs() -> None:
    """With a root handler already installed (e.g. by a log forwarder), app
    records must be written once, not once per handler."""
    code = """
import logging
from unittest.mock import MagicMock, patch

logging.basicConfig(format="ROOT %(message)s")

with (
    patch("scripts.run_worker.redis.from_url"),
    patch("scripts.run_worker.Worker", return_value=MagicMock()),
    patch("app.db.engine.dispose"),
):
    from scripts.run_worker import _run_single_worker

    _run_single_worker(["light"], "redis://localhost:6379/0", None, burst=True)

logging.getLogger("app.email").info("verify link once")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("verify link once") == 1
