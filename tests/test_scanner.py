"""Scanner tests: polling, stability, dedup, recovery, availability."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import timedelta
from pathlib import Path

from app.config import Settings
from app.database import Database
from app.models import (
    STATUS_ERROR,
    STATUS_PROCESSING,
    STATUS_SORTED,
    BarcodeMapping,
    Document,
    utc_now,
)
from app.services.document_processor import DocumentProcessor
from app.services.scanner import ScannerService
from tests.helpers import save_barcode, snapshot_tree


def add_mapping(db: Database, barcode: str, surname: str) -> None:
    with db.session() as session:
        session.add(BarcodeMapping(barcode=barcode, surname=surname))


async def test_scanner_processes_new_document_and_is_idempotent(
    scanner: ScannerService, settings: Settings, db: Database
) -> None:
    add_mapping(db, "SCAN-1", "Иванов")
    nested = settings.input_dir / "nested"
    nested.mkdir()
    save_barcode(nested / "scan.png", "SCAN-1")
    before = snapshot_tree(settings.input_dir)

    first = await scanner.scan_once()
    assert first["new"] == 1
    assert first["processed"] == 1
    assert first["errors"] == 0
    assert scanner.status()["input_available"] is True

    copies = list((settings.output_dir / "Иванов").iterdir())
    assert len(copies) == 1

    second = await scanner.scan_once()
    assert second == {"new": 0, "processed": 0, "errors": 0}
    assert len(list((settings.output_dir / "Иванов").iterdir())) == 1
    assert snapshot_tree(settings.input_dir) == before


async def test_unstable_file_is_not_processed(
    scanner: ScannerService, settings: Settings, db: Database, tmp_path: Path
) -> None:
    settings.file_stability_checks = 3
    settings.file_stability_interval = 0.2
    add_mapping(db, "SLOW-1", "Иванов")
    source = settings.input_dir / "slow.png"
    source.write_bytes(b"partial data")
    before = snapshot_tree(settings.input_dir)

    task = asyncio.create_task(scanner.scan_once())
    await asyncio.sleep(0.05)
    source.write_bytes(b"partial data changed while scanning")
    counts = await task

    assert counts["processed"] == 0
    surname_dir = settings.output_dir / "Иванов"
    assert not surname_dir.exists() or not list(surname_dir.iterdir())
    # The scanner must not modify the source either.
    assert set(snapshot_tree(settings.input_dir)) == set(before)

    # A complete, stable file is processed on a subsequent pass.
    save_barcode(source, "SLOW-1")
    counts = await scanner.scan_once()
    assert counts["processed"] == 1


async def test_missing_input_directory_does_not_crash(
    scanner: ScannerService, settings: Settings, db: Database
) -> None:
    add_mapping(db, "NET-1", "Петров")
    backup = settings.input_dir.with_name("input_offline")
    settings.input_dir.rename(backup)
    try:
        counts = await scanner.scan_once()
        assert counts == {"new": 0, "processed": 0, "errors": 0}
        assert scanner.status()["input_available"] is False
        assert scanner.status()["last_error"]
    finally:
        backup.rename(settings.input_dir)

    save_barcode(settings.input_dir / "after_offline.png", "NET-1")
    counts = await scanner.scan_once()
    assert counts["processed"] == 1
    assert scanner.status()["input_available"] is True


async def test_damaged_document_does_not_stop_other_documents(
    scanner: ScannerService, settings: Settings, db: Database
) -> None:
    add_mapping(db, "OK-1", "Сидоров")
    (settings.input_dir / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
    save_barcode(settings.input_dir / "good.png", "OK-1")

    counts = await scanner.scan_once()
    assert counts["processed"] == 2
    assert counts["errors"] == 1

    with db.session() as session:
        statuses = {row.status for row in session.query(Document).all()}
    assert statuses == {STATUS_ERROR, STATUS_SORTED}
    assert len(list((settings.output_dir / "Сидоров").iterdir())) == 1


async def test_retry_backoff_prevents_tight_retry_loop(
    scanner: ScannerService,
    settings: Settings,
    db: Database,
    processor: DocumentProcessor,
    monkeypatch,
) -> None:
    from app.services.errors import ProcessingError

    def failing_extract(path, deadline=None):
        raise ProcessingError("Временная ошибка чтения", retryable=True)

    monkeypatch.setattr(processor.reader, "extract", failing_extract)
    save_barcode(settings.input_dir / "temporary.png", "RETRY-1")
    first = await scanner.scan_once()
    assert first["errors"] == 1
    with db.session() as session:
        document = session.query(Document).one()
        first_attempts = document.attempts
        assert document.status == STATUS_ERROR
        assert document.next_retry_at is not None

    # Backoff must prevent an immediate second attempt.
    second = await scanner.scan_once()
    assert second["processed"] == 0
    with db.session() as session:
        document = session.query(Document).one()
        assert document.attempts == first_attempts


async def test_non_retryable_document_is_not_retried_forever(
    scanner: ScannerService, settings: Settings, db: Database
) -> None:
    from tests.helpers import save_encrypted_pdf

    save_encrypted_pdf(settings.input_dir / "encrypted.pdf")
    first = await scanner.scan_once()
    assert first["errors"] == 1
    with db.session() as session:
        document = session.query(Document).one()
        assert document.attempts == settings.max_attempts
    second = await scanner.scan_once()
    assert second["processed"] == 0


async def test_stale_processing_state_is_recovered(
    scanner: ScannerService, settings: Settings, db: Database
) -> None:
    add_mapping(db, "REC-1", "Иванов")
    source = save_barcode(settings.input_dir / "recovered.png", "REC-1")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stat = source.stat()
    with db.session() as session:
        session.add(
            Document(
                source_path=source.name,
                file_name=source.name,
                sha256=digest,
                size_bytes=stat.st_size,
                source_mtime_ns=stat.st_mtime_ns,
                status=STATUS_PROCESSING,
                attempts=1,
                updated_at=utc_now() - timedelta(hours=2),
            )
        )

    counts = await scanner.scan_once()
    assert counts["processed"] == 1
    with db.session() as session:
        document = session.query(Document).one()
        assert document.status == STATUS_SORTED
        assert document.attempts == 2
    assert len(list((settings.output_dir / "Иванов").iterdir())) == 1


async def test_recover_interrupted_on_startup(settings: Settings, db: Database) -> None:
    source = save_barcode(settings.input_dir / "interrupted.png", "X")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stat = source.stat()
    with db.session() as session:
        session.add(
            Document(
                source_path=source.name,
                file_name=source.name,
                sha256=digest,
                size_bytes=stat.st_size,
                source_mtime_ns=stat.st_mtime_ns,
                status=STATUS_PROCESSING,
            )
        )
    assert db.recover_interrupted() == 1
    with db.session() as session:
        assert session.query(Document).one().status == "pending"


async def test_concurrent_scan_requests_do_not_overlap(
    scanner: ScannerService, settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "LOCK-1", "Иванов")
    save_barcode(settings.input_dir / "lock.png", "LOCK-1")
    real_process = processor.process

    def slow_process(path: Path):
        time.sleep(0.3)
        return real_process(path)

    processor.process = slow_process  # type: ignore[method-assign]
    first_task = asyncio.create_task(scanner.scan_once())
    await asyncio.sleep(0.05)
    second = await scanner.scan_once()
    first = await first_task
    assert second == {"new": 0, "processed": 0, "errors": 0}
    assert first["processed"] == 1
    assert len(list((settings.output_dir / "Иванов").iterdir())) == 1


async def test_scanner_start_pause_resume_and_manual_scan(
    scanner: ScannerService, settings: Settings, db: Database
) -> None:
    add_mapping(db, "CTRL-1", "Иванов")
    settings.scan_interval = 0.1
    await scanner.start()
    try:
        scanner.pause()
        assert scanner.paused is True
        assert scanner.status()["running"] is True
        save_barcode(settings.input_dir / "paused.png", "CTRL-1")
        await asyncio.sleep(0.3)
        surname_dir = settings.output_dir / "Иванов"
        assert not surname_dir.exists() or not list(surname_dir.iterdir())

        # Manual one-shot scan is allowed while automatic scanning is paused.
        assert scanner.request_scan() is True
        for _ in range(50):
            if surname_dir.exists() and list(surname_dir.iterdir()):
                break
            await asyncio.sleep(0.05)
        assert len(list(surname_dir.iterdir())) == 1
        assert scanner.paused is True

        scanner.resume()
        assert scanner.paused is False
    finally:
        await scanner.stop()
    assert scanner.running is False


async def test_scanner_ignores_hidden_and_unsupported_files(
    scanner: ScannerService, settings: Settings
) -> None:
    (settings.input_dir / ".hidden.png").write_bytes(b"x")
    (settings.input_dir / "._resource.png").write_bytes(b"x")
    (settings.input_dir / "document.docx").write_bytes(b"x")
    (settings.input_dir / "archive.png.part").write_bytes(b"x")
    counts = await scanner.scan_once()
    assert counts == {"new": 0, "processed": 0, "errors": 0}
