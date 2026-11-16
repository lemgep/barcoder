"""Application configuration loaded from environment variables."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    All values come from environment variables.  The application deliberately
    does not load a .env file by itself: docker-compose passes the file through
    ``env_file`` so that the same configuration mechanism is used everywhere.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
        env_file=None,
    )

    # --- paths -------------------------------------------------------------
    input_dir: Path = Path("/app/input")
    output_dir: Path = Path("/app/output")
    data_dir: Path = Path("/app/data")
    log_dir: Path = Path("/app/logs")

    # --- scanner -----------------------------------------------------------
    scan_interval: float = Field(default=10.0, gt=0, le=86400)
    file_stability_checks: int = Field(default=3, ge=2, le=20)
    file_stability_interval: float = Field(default=5.0, gt=0, le=300)
    max_file_workers: int = Field(default=2, ge=1, le=16)
    max_attempts: int = Field(default=5, ge=1, le=50)
    retry_base_seconds: int = Field(default=60, ge=1, le=86400)
    retry_max_seconds: int = Field(default=3600, ge=1, le=604800)
    stale_processing_seconds: int = Field(default=900, ge=30, le=86400)
    scanner_enabled: bool = True
    # Only documents modified within this window are processed. 0 disables the
    # filter and processes every supported file in INPUT_DIR (useful for a
    # one-off archive import). Applies on top of SHA-256 deduplication.
    process_files_newer_than_hours: float = Field(default=24.0, ge=0, le=8760)

    # --- document processing ----------------------------------------------
    pdf_dpi: int = Field(default=200, ge=72, le=600)
    max_pdf_pages: int = Field(default=100, ge=1, le=5000)
    max_document_seconds: float = Field(default=90.0, gt=1, le=3600)
    max_image_pixels: int = Field(default=80_000_000, ge=1_000_000)
    barcode_formats: str = "Code128,Code39,EAN13,EAN8,QRCode,DataMatrix,PDF417,ITF"

    # --- web ---------------------------------------------------------------
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, ge=1, le=65535)
    web_username: str = "admin"
    web_password: SecretStr | None = None
    web_password_hash: str | None = None
    session_secret: SecretStr | None = None
    session_https_only: bool = False
    auth_enabled: bool = True
    max_csv_rows: int = Field(default=100_000, ge=1)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    allowed_hosts: str = "*"

    # --- misc --------------------------------------------------------------
    tz: str = Field(
        default="Europe/Moscow",
        validation_alias=AliasChoices("TZ", "APP_TZ", "TIMEZONE"),
    )
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper().strip()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL")
        return level

    @field_validator("web_username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise ValueError("WEB_USERNAME must be non-empty and at most 128 characters")
        return value

    @field_validator("tz")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _validate_security(self) -> Settings:
        if self.auth_enabled:
            has_password = bool(
                self.web_password is not None and self.web_password.get_secret_value()
            )
            if not has_password and not self.web_password_hash:
                raise ValueError(
                    "WEB_PASSWORD or WEB_PASSWORD_HASH must be a non-empty value "
                    "when authentication is enabled"
                )
        if self.web_password_hash and not self.web_password_hash.startswith(("pbkdf2_sha256$", "scrypt$")):
            raise ValueError("WEB_PASSWORD_HASH has an unsupported format")
        return self

    # --- derived helpers ---------------------------------------------------
    @property
    def database_path(self) -> Path:
        return self.data_dir / "barcode_sorter.db"

    @property
    def barcode_format_list(self) -> list[str]:
        return [item.strip() for item in self.barcode_formats.split(",") if item.strip()]

    def resolved_paths(self) -> dict[str, Path]:
        return {
            "input": self.input_dir.expanduser().resolve(strict=False),
            "output": self.output_dir.expanduser().resolve(strict=False),
            "data": self.data_dir.expanduser().resolve(strict=False),
            "logs": self.log_dir.expanduser().resolve(strict=False),
        }

    def validate_layout(self) -> None:
        """Refuse configurations where the application could write into input.

        The input directory is mounted read-only by Docker, but this additional
        application-level check protects against misconfiguration and local
        runs without Docker.
        """

        paths = self.resolved_paths()
        input_path = paths["input"]

        for name in ("output", "data", "logs"):
            other = paths[name]
            if other == input_path or other.is_relative_to(input_path):
                raise ValueError(
                    f"Unsafe configuration: {name.upper()}_DIR ({other}) is inside "
                    f"INPUT_DIR ({input_path}). The input directory must never be written to."
                )
            if input_path.is_relative_to(other):
                raise ValueError(
                    f"Unsafe configuration: INPUT_DIR ({input_path}) is inside "
                    f"{name.upper()}_DIR ({other}). Refusing to start."
                )

        if input_path == paths["output"]:
            raise ValueError("INPUT_DIR and OUTPUT_DIR must not be the same directory")

    def ensure_runtime_dirs(self) -> None:
        """Create only writable runtime directories, never the input directory."""

        self.validate_layout()
        for path in (self.output_dir, self.data_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o750)
        with contextlib.suppress(OSError):
            os.chmod(self.data_dir, 0o700)

    def logging_level(self) -> int:
        return getattr(logging, self.log_level, logging.INFO)


def load_settings() -> Settings:
    return Settings()
