"""Processor and safe-copy tests, including input immutability."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.config import Settings
from app.constants import DIR_NEEDS_REVIEW, DIR_UNMATCHED, DIR_UNREADABLE
from app.database import Database
from app.models import (
    STATUS_ERROR,
    STATUS_NEEDS_REVIEW,
    STATUS_SORTED,
    STATUS_UNMATCHED,
    STATUS_UNREADABLE,
    BarcodeMapping,
    Document,
)
from app.services.barcode_reader import BarcodeReader
from app.services.document_processor import DocumentProcessor
from app.services.errors import ProcessingError
from app.services.file_copier import FileCopier, sanitize_filename, sanitize_surname
from tests.helpers import (
    save_barcode,
    save_multi_barcode_image,
    save_pdf_with_barcode,
    save_pdf_without_barcode,
    snapshot_tree,
)


def build_processor(settings: Settings, db: Database) -> DocumentProcessor:
    return DocumentProcessor(settings, db, BarcodeReader(settings), FileCopier(settings))


def add_mapping(db: Database, barcode: str, surname: str) -> None:
    with db.session() as session:
        session.add(BarcodeMapping(barcode=barcode, surname=surname))


@pytest.fixture
def processor(settings: Settings, db: Database) -> DocumentProcessor:
    return build_processor(settings, db)


def test_sorted_document_and_input_untouched(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "DOC-001", "Иванов")
    source = save_pdf_with_barcode(settings.input_dir / "scan_001.pdf", "DOC-001")
    before = snapshot_tree(settings.input_dir)
    source_before = (source.stat().st_size, source.stat().st_mtime_ns, source.read_bytes())

    outcome = processor.process(source)

    assert outcome.status == STATUS_SORTED
    assert outcome.surname == "Иванов"
    assert outcome.target_path is not None
    target = Path(outcome.target_path)
    assert target == settings.output_dir / "Иванов" / "scan_001.pdf"
    assert target.read_bytes() == source_before[2]

    # The source directory must be byte-for-byte unchanged, including no new files.
    assert snapshot_tree(settings.input_dir) == before
    assert (source.stat().st_size, source.stat().st_mtime_ns, source.read_bytes()) == source_before

    with db.session() as session:
        document = session.query(Document).one()
        assert document.status == STATUS_SORTED
        assert document.barcodes is not None
        assert "DOC-001" in document.barcodes


def test_input_directory_is_read_only_during_processing(
    settings: Settings, db: Database
) -> None:
    add_mapping(db, "RO-001", "Петров")
    source = save_barcode(settings.input_dir / "scan.png", "RO-001")
    original_mode = stat.S_IMODE(settings.input_dir.stat().st_mode)
    os.chmod(settings.input_dir, 0o555)
    before = snapshot_tree(settings.input_dir)
    try:
        assert not os.access(settings.input_dir, os.W_OK)
        outcome = build_processor(settings, db).process(source)
        assert outcome.status == STATUS_SORTED
        assert snapshot_tree(settings.input_dir) == before
    finally:
        os.chmod(settings.input_dir, original_mode)


def test_unknown_barcode_goes_to_unmatched(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    source = save_barcode(settings.input_dir / "unknown.png", "NO-SUCH-CODE")
    outcome = processor.process(source)
    assert outcome.status == STATUS_UNMATCHED
    assert Path(outcome.target_path).parent == settings.output_dir / DIR_UNMATCHED


def test_document_without_barcode_goes_to_unreadable(
    settings: Settings, processor: DocumentProcessor
) -> None:
    source = save_pdf_without_barcode(settings.input_dir / "blank.pdf")
    outcome = processor.process(source)
    assert outcome.status == STATUS_UNREADABLE
    assert Path(outcome.target_path).parent == settings.output_dir / DIR_UNREADABLE


def test_multiple_different_surnames_need_review(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "AAA-1", "Иванов")
    add_mapping(db, "BBB-2", "Петров")
    source = save_multi_barcode_image(
        settings.input_dir / "multi.png", [("AAA-1", "code128"), ("BBB-2", "qr")]
    )
    outcome = processor.process(source)
    assert outcome.status == STATUS_NEEDS_REVIEW
    assert outcome.surname is None
    assert Path(outcome.target_path).parent == settings.output_dir / DIR_NEEDS_REVIEW


def test_multiple_barcodes_same_surname_are_sorted(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "SAME-1", "Сидоров")
    add_mapping(db, "SAME-2", "Сидоров")
    source = save_multi_barcode_image(
        settings.input_dir / "same.png", [("SAME-1", "code128"), ("SAME-2", "qr")]
    )
    outcome = processor.process(source)
    assert outcome.status == STATUS_SORTED
    assert outcome.surname == "Сидоров"


def test_existing_file_is_never_overwritten(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "COLL-1", "Иванов")
    target_dir = settings.output_dir / "Иванов"
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = target_dir / "scan.png"
    existing.write_bytes(b"existing content")

    source = save_barcode(settings.input_dir / "scan.png", "COLL-1")
    outcome = processor.process(source)

    assert outcome.status == STATUS_SORTED
    assert existing.read_bytes() == b"existing content"
    assert Path(outcome.target_path).name == "scan_1.png"
    assert Path(outcome.target_path).read_bytes() == source.read_bytes()


def test_repeated_processing_creates_no_extra_copy(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "ONCE-1", "Иванов")
    source = save_barcode(settings.input_dir / "once.png", "ONCE-1")
    first = processor.process(source)
    second = processor.process(source)
    assert first.status == STATUS_SORTED
    assert second.skipped is True
    copies = list((settings.output_dir / "Иванов").iterdir())
    assert len(copies) == 1


def test_processor_with_fresh_instance_skips_completed_document(
    settings: Settings, db: Database
) -> None:
    add_mapping(db, "RESTART-1", "Иванов")
    source = save_barcode(settings.input_dir / "restart.png", "RESTART-1")
    assert build_processor(settings, db).process(source).status == STATUS_SORTED

    # Simulate a container restart: fresh reader/copier/processor, same DB.
    outcome = build_processor(settings, db).process(source)
    assert outcome.skipped is True
    assert len(list((settings.output_dir / "Иванов").iterdir())) == 1


def test_barcode_with_leading_zeros_is_not_numeric(
    settings: Settings, db: Database, processor: DocumentProcessor
) -> None:
    add_mapping(db, "0000123456708", "Ведущие нули")
    source = save_barcode(settings.input_dir / "zeros.png", "0000123456708")
    outcome = processor.process(source)
    assert outcome.status == STATUS_SORTED
    assert outcome.matched_barcode == "0000123456708"


def test_empty_or_invalid_surname_is_rejected() -> None:
    for bad in [
        "",
        "..",
        "../evil",
        "a/b",
        "a\\b",
        "CON",
        ".hidden",
        "name.",
        "UNMATCHED",
        "NEEDS_REVIEW",
    ]:
        with pytest.raises(ValueError):
            sanitize_surname(bad)


def test_filename_sanitising_removes_traversal() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\temp\\evil.pdf") == "evil.pdf"
    assert sanitize_filename("...") == "document"


def test_copier_rejects_symlink_in_input(settings: Settings) -> None:
    copier = FileCopier(settings)
    real = settings.input_dir / "real.png"
    real.write_bytes(b"x")
    link = settings.input_dir / "link.png"
    link.symlink_to(real)
    with pytest.raises(ProcessingError):
        copier.copy_to_directory(link, "Иванов")


def test_copier_refuses_symlinked_output_directory(
    settings: Settings, db: Database
) -> None:
    copier = FileCopier(settings)
    outside = settings.input_dir / "escape"
    outside.mkdir()
    (settings.output_dir / "Иванов").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProcessingError):
        copier.copy_to_directory(save_barcode(settings.input_dir / "x.png", "X"), "Иванов")


def test_hash_mismatch_retry_when_file_changes(
    settings: Settings, db: Database, processor: DocumentProcessor, monkeypatch
) -> None:
    add_mapping(db, "CH-1", "Иванов")
    source = save_barcode(settings.input_dir / "changing.png", "CH-1")

    original = processor.reader.extract
    called = {"value": False}

    def changing_extract(path, deadline=None):
        if not called["value"]:
            called["value"] = True
            with path.open("ab") as handle:
                handle.write(b"changed")
        return original(path, deadline=deadline)

    monkeypatch.setattr(processor.reader, "extract", changing_extract)
    outcome = processor.process(source)
    assert outcome.status == STATUS_ERROR
    assert "изменился" in (outcome.error or "").lower()
    with db.session() as session:
        document = session.query(Document).one()
        assert document.status == STATUS_ERROR
        # The name is reserved before copying, but no file was published.
        assert document.target_path is not None
        assert not Path(document.target_path).exists()


def test_retry_after_db_failure_reuses_published_copy(
    settings: Settings, db: Database, processor: DocumentProcessor, monkeypatch
) -> None:
    add_mapping(db, "CRASH-1", "Иванов")
    source = save_barcode(settings.input_dir / "crash.png", "CRASH-1")
    original_store = processor._store_success

    def failing_store(**kwargs):
        raise ProcessingError("Имитация сбоя записи в БД", retryable=True)

    monkeypatch.setattr(processor, "_store_success", failing_store)
    first = processor.process(source)
    assert first.status == STATUS_ERROR
    surname_dir = settings.output_dir / "Иванов"
    files_after_copy = list(surname_dir.iterdir())
    assert len(files_after_copy) == 1
    target_path = files_after_copy[0]

    # Simulate a restart/retry: no duplicate copy must be created.
    monkeypatch.setattr(processor, "_store_success", original_store)
    second = processor.process(source)
    assert second.status == STATUS_SORTED
    assert list(surname_dir.iterdir()) == [target_path]
    assert Path(second.target_path) == target_path
