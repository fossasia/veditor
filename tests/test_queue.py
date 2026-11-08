from pathlib import Path

import yaml

from app.consts import JobKind
from app.tasks import STAGE_CONFIG


def test_stage_config_contains_all_stages() -> None:
    expected_stages = {
        JobKind.INGEST,
        JobKind.DETECT,
        JobKind.CUT,
        JobKind.INTRO,
        JobKind.OUTRO,
        JobKind.CONCAT,
        JobKind.PREVIEW,
        JobKind.LOUDNESS,
        JobKind.TRANSCODE,
        JobKind.PUBLISH,
    }
    assert set(STAGE_CONFIG.keys()) == expected_stages


def test_stage_config_queue_assignments() -> None:
    for cfg in STAGE_CONFIG.values():
        assert "queue" in cfg
        assert "job_timeout" in cfg
        assert isinstance(cfg["job_timeout"], int)
        assert cfg["job_timeout"] > 0

    assert STAGE_CONFIG[JobKind.INGEST]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.DETECT]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.CUT]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.INTRO]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.OUTRO]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.CONCAT]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.PREVIEW]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.LOUDNESS]["queue"] == "light"
    assert STAGE_CONFIG[JobKind.TRANSCODE]["queue"] == "heavy"
    assert STAGE_CONFIG[JobKind.PUBLISH]["queue"] == "light"


def _job_timeout(stage: JobKind) -> int:
    """Narrow a stage's job_timeout to int so it can be compared numerically."""
    timeout = STAGE_CONFIG[stage]["job_timeout"]
    assert isinstance(timeout, int)
    return timeout


def test_transcode_timeout_profile() -> None:
    # Heavy transcode timeout must be significantly longer than light stages
    assert _job_timeout(JobKind.TRANSCODE) >= 3600
    assert _job_timeout(JobKind.DETECT) <= 600
    assert _job_timeout(JobKind.CUT) <= 1800
    assert _job_timeout(JobKind.INTRO) <= 600
    assert _job_timeout(JobKind.OUTRO) <= 600
    assert _job_timeout(JobKind.CONCAT) <= 1800


def test_stage_config_exact_timeouts() -> None:
    expected_timeouts = {
        JobKind.DETECT: 300,
        JobKind.CUT: 900,
        JobKind.INTRO: 300,
        JobKind.OUTRO: 300,
        JobKind.CONCAT: 1800,
        JobKind.PREVIEW: 1800,
        JobKind.LOUDNESS: 900,
        JobKind.TRANSCODE: 14400,
        JobKind.PUBLISH: 300,
    }
    for stage, expected_timeout in expected_timeouts.items():
        assert STAGE_CONFIG[stage]["job_timeout"] == expected_timeout


def test_docker_compose_worker_concurrency() -> None:
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.is_file()

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "worker-light" in services
    assert "worker-heavy" in services

    light_cmd = services["worker-light"]["command"]
    heavy_cmd = services["worker-heavy"]["command"]

    assert "--concurrency" in light_cmd
    assert "--concurrency" in heavy_cmd


def test_docker_compose_postgres_volume_mount() -> None:
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.is_file()

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "postgres" in services

    postgres_volumes = services["postgres"].get("volumes", [])
    assert "postgres_data:/var/lib/postgresql" in postgres_volumes
