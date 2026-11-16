"""Security primitive tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging_config import configure_logging
from app.security import (
    LoginRateLimiter,
    get_session_secret,
    hash_password,
    verify_password,
    verify_password_hash,
)


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("s3cret!", iterations=100_000)
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password_hash("s3cret!", encoded) is True
    assert verify_password_hash("wrong", encoded) is False


def test_malformed_password_hash_fails_closed() -> None:
    assert verify_password_hash("x", "invalid") is False
    assert verify_password_hash("x", "pbkdf2_sha256$1$bad$bad") is False


def test_plain_password_from_environment(tmp_path: Path) -> None:
    settings = Settings(
        input_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        web_password="env-password",
        session_secret="x" * 40,
    )
    assert verify_password("env-password", settings) is True
    assert verify_password("nope", settings) is False


def test_hash_only_configuration(tmp_path: Path) -> None:
    encoded = hash_password("hash-password", iterations=100_000)
    settings = Settings(
        input_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        web_password_hash=encoded,
        session_secret="x" * 40,
    )
    assert verify_password("hash-password", settings) is True
    assert verify_password("wrong", settings) is False


def test_session_secret_is_persisted_with_private_mode(tmp_path: Path) -> None:
    settings = Settings(
        input_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        web_password="x",
        session_secret=None,
    )
    settings.ensure_runtime_dirs()
    first = get_session_secret(settings)
    second = get_session_secret(settings)
    assert first == second
    assert len(first) >= 32
    secret_path = settings.data_dir / ".session_secret"
    assert secret_path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_empty_password_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            input_dir=tmp_path / "in",
            output_dir=tmp_path / "out",
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            web_password="",
            session_secret="x" * 40,
        )


def test_log_file_has_restricted_permissions(tmp_path: Path) -> None:
    settings = Settings(
        input_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        web_password="x",
        session_secret="x" * 40,
        log_level="WARNING",
    )
    settings.ensure_runtime_dirs()
    configure_logging(settings)
    log_path = settings.log_dir / "barcode-sorter.log"
    assert log_path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o640


def test_login_rate_limiter() -> None:
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60)
    assert limiter.is_blocked("1.2.3.4") is False
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is False
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True
    assert limiter.is_blocked("5.6.7.8") is False
    limiter.reset("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is False
