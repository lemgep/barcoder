"""Static validation of Docker configuration (no daemon required)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_compose_input_mount_is_read_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["barcode-sorter"]
    volumes = service["volumes"]
    input_mounts = [volume for volume in volumes if ":/app/input" in volume]
    assert input_mounts, "input volume is missing"
    assert len(input_mounts) == 1
    assert input_mounts[0].endswith(":ro"), "input must be mounted read-only"

    # No other volume may make input writable or mount the Docker socket.
    assert not any("/var/run/docker.sock" in volume for volume in volumes)
    assert not any(":/app/input:ro," in volume for volume in volumes)
    assert any(":/app/output" in volume and ":ro" not in volume for volume in volumes)
    assert any(":/app/data" in volume for volume in volumes)
    assert any(":/app/logs" in volume for volume in volumes)


def test_compose_security_and_restart_policy() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["barcode-sorter"]
    assert service["restart"] == "unless-stopped"
    assert service["read_only"] is True
    assert service.get("privileged") is not True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["init"] is True
    assert service["healthcheck"]["test"] == ["CMD", "python", "-m", "app.healthcheck"]
    assert service["stop_grace_period"] == "45s"
    assert any("size=" in entry for entry in service["tmpfs"])
    assert service["environment"]["INPUT_DIR"] == "/app/input"
    assert service["environment"]["DATA_DIR"] == "/app/data"


def test_dockerfile_uses_non_root_and_readonly_friendly_settings() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12" in dockerfile
    assert "USER app" in dockerfile
    assert "USER root" not in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "privileged" not in dockerfile.lower()


def test_env_example_contains_required_settings() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    required = {
        "INPUT_DIR",
        "OUTPUT_DIR",
        "DATA_DIR",
        "SCAN_INTERVAL",
        "FILE_STABILITY_CHECKS",
        "FILE_STABILITY_INTERVAL",
        "PDF_DPI",
        "MAX_PDF_PAGES",
        "WEB_HOST",
        "WEB_PORT",
        "WEB_USERNAME",
        "WEB_PASSWORD",
        "LOG_LEVEL",
        "TZ",
    }
    assert required <= set(values)
    assert values["WEB_PASSWORD"] != ""
    assert values["TZ"] == "Europe/Moscow"


def test_dockerignore_excludes_secrets_and_test_data() -> None:
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore
    assert "data" in ignore
    assert "logs" in ignore
    assert "tests" in ignore


def test_gitignore_excludes_env() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
