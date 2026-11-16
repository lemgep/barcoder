"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database
from app.services.barcode_reader import BarcodeReader
from app.services.document_processor import DocumentProcessor
from app.services.file_copier import FileCopier
from app.services.scanner import ScannerService


def build_settings(tmp_path: Path, **overrides) -> Settings:
    input_dir = tmp_path / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "input_dir": input_dir,
        "output_dir": tmp_path / "output",
        "data_dir": tmp_path / "data",
        "log_dir": tmp_path / "logs",
        "web_username": "admin",
        "web_password": "test-password",
        "session_secret": "test-session-secret-with-enough-entropy",
        "scanner_enabled": False,
        "scan_interval": 0.05,
        "file_stability_checks": 2,
        "file_stability_interval": 0.01,
        "pdf_dpi": 150,
        "tz": "UTC",
        "log_level": "WARNING",
        "max_attempts": 3,
        "retry_base_seconds": 1,
        "retry_max_seconds": 2,
        "max_file_workers": 2,
        "max_document_seconds": 10,
        "max_pdf_pages": 10,
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.ensure_runtime_dirs()
    return settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def db(settings: Settings):
    database = Database(settings)
    database.init_schema()
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def input_dir(settings: Settings) -> Path:
    return settings.input_dir


@pytest.fixture
def reader(settings: Settings) -> BarcodeReader:
    return BarcodeReader(settings)


@pytest.fixture
def processor(settings: Settings, db: Database, reader: BarcodeReader) -> DocumentProcessor:
    return DocumentProcessor(settings, db, reader, FileCopier(settings))


@pytest.fixture
def scanner(settings: Settings, db: Database, processor: DocumentProcessor) -> ScannerService:
    return ScannerService(settings, db, processor)
