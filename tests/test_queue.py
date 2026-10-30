from pathlib import Path

import yaml

from app.tasks import STAGE_CONFIG


def test_stage_config_contains_all_stages() -> None:
    expected_stages = {
        "ingest",
        "detect",
        "cut",
        "intro",
        "outro",
        "concat",
        "preview",
        "loudness",
        "transcode",
        "publish",
    }
    assert set(STAGE_CONFIG.keys()) == expected_stages


def test_stage_config_queue_assignments() -> None:
    for cfg in STAGE_CONFIG.values():
        assert "queue" in cfg
        assert "job_timeout" in cfg
        assert isinstance(cfg["job_timeout"], int)
        assert cfg["job_timeout"] > 0

    assert STAGE_CONFIG["ingest"]["queue"] == "light"
    assert STAGE_CONFIG["detect"]["queue"] == "light"
    assert STAGE_CONFIG["cut"]["queue"] == "light"
    assert STAGE_CONFIG["intro"]["queue"] == "light"
    assert STAGE_CONFIG["outro"]["queue"] == "light"
    assert STAGE_CONFIG["concat"]["queue"] == "light"
    assert STAGE_CONFIG["preview"]["queue"] == "light"
    assert STAGE_CONFIG["loudness"]["queue"] == "light"
    assert STAGE_CONFIG["transcode"]["queue"] == "heavy"
    assert STAGE_CONFIG["publish"]["queue"] == "light"


def _job_timeout(stage: str) -> int:
    """Narrow a stage's job_timeout to int so it can be compared numerically."""
    timeout = STAGE_CONFIG[stage]["job_timeout"]
    assert isinstance(timeout, int)
    return timeout


def test_transcode_timeout_profile() -> None:
    # Heavy transcode timeout must be significantly longer than light stages
    assert _job_timeout("transcode") >= 3600
    assert _job_timeout("detect") <= 600
    assert _job_timeout("cut") <= 1800
    assert _job_timeout("intro") <= 600
    assert _job_timeout("outro") <= 600
    assert _job_timeout("concat") <= 1800


def test_stage_config_exact_timeouts() -> None:
    expected_timeouts = {
        "detect": 300,
        "cut": 900,
        "intro": 300,
        "outro": 300,
        "concat": 1800,
        "preview": 1800,
        "loudness": 900,
        "transcode": 14400,
        "publish": 300,
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
