"""Document processing pipeline: hash, barcode, mapping, copy, audit."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.constants import (
    DIR_NEEDS_REVIEW,
    DIR_UNMATCHED,
    DIR_UNREADABLE,
    SERVICE_DIRECTORIES,
)
from app.database import Database
from app.models import (
    STATUS_ERROR,
    STATUS_NEEDS_REVIEW,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SORTED,
    STATUS_UNMATCHED,
    STATUS_UNREADABLE,
    TERMINAL_STATUSES,
    BarcodeMapping,
    Document,
    ProcessingLog,
    utc_now,
)
from app.services.barcode_reader import BarcodeReader
from app.services.errors import ProcessingError
from app.services.file_copier import FileCopier, TargetExistsError, sanitize_component

logger = logging.getLogger(__name__)

MAX_BARCODES_PER_DOCUMENT = 100
MAX_BARCODE_LENGTH = 1024


class SourceChangedError(ProcessingError):
    def __init__(self) -> None:
        super().__init__(
            "Исходный файл изменился во время обработки; попытка будет повторена",
            retryable=True,
        )


@dataclass
class ProcessOutcome:
    status: str
    document_id: int | None = None
    barcodes: list[str] = field(default_factory=list)
    matched_barcode: str | None = None
    surname: str | None = None
    target_path: str | None = None
    error: str | None = None
    skipped: bool = False
    retry_at: datetime | None = None

    @property
    def success(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class SourceInfo:
    relative_path: str
    absolute_path: Path
    sha256: str
    size: int
    mtime_ns: int


class DocumentProcessor:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        reader: BarcodeReader,
        copier: FileCopier,
    ) -> None:
        self.settings = settings
        self.db = database
        self.reader = reader
        self.copier = copier
        self.input_root = settings.input_dir.expanduser().resolve(strict=False)
        self.output_root = settings.output_dir.expanduser().resolve(strict=False)
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    # ---------------------------------------------------------------- public
    def process(self, source: Path) -> ProcessOutcome:
        source = Path(source)
        key = str(source)
        with self._inflight_lock:
            if key in self._inflight:
                return ProcessOutcome(status=STATUS_PENDING, skipped=True)
            self._inflight.add(key)
        try:
            return self._process(source)
        except Exception as exc:
            logger.exception("Unhandled error while processing %s", source)
            return ProcessOutcome(
                status=STATUS_ERROR,
                error=f"Непредвиденная ошибка обработки: {exc}",
            )
        finally:
            with self._inflight_lock:
                self._inflight.discard(key)

    def process_relative(self, relative_path: str) -> ProcessOutcome:
        absolute = self.input_root / relative_path
        return self.process(absolute)

    # -------------------------------------------------------------- pipeline
    def _process(self, source: Path) -> ProcessOutcome:
        source = self.copier.assert_source(source)
        try:
            info = self._hash_source(source)
        except SourceChangedError as exc:
            logger.info("%s", exc.message)
            return ProcessOutcome(status=STATUS_PENDING, error=exc.message)

        document = self._begin_document(info)
        if document is None:
            return ProcessOutcome(status=STATUS_PROCESSING, skipped=True)

        doc_id = document.id
        try:
            deadline = time.monotonic() + self.settings.max_document_seconds
            extraction = self.reader.extract(source, deadline=deadline)
            values = self._normalize_barcodes(extraction.unique_values)
            warnings = list(extraction.warnings)

            if not values:
                return self._finalize_with_copy(
                    doc_id=doc_id,
                    source=source,
                    source_info=info,
                    status=STATUS_UNREADABLE,
                    directory=DIR_UNREADABLE,
                    barcodes=[],
                    matched_barcode=None,
                    surname=None,
                    message="Штрихкод не обнаружен"
                    + (f". {'; '.join(warnings)}" if warnings else ""),
                    error_note="; ".join(warnings) or None,
                )

            mapping = self._lookup_mappings(values)
            matched = {barcode: surname for barcode, surname in mapping.items() if barcode in values}
            unique_surnames: list[str] = []
            for surname in matched.values():
                safe_surname = sanitize_component(surname)
                if safe_surname not in unique_surnames:
                    unique_surnames.append(safe_surname)

            if not matched:
                return self._finalize_with_copy(
                    doc_id=doc_id,
                    source=source,
                    source_info=info,
                    status=STATUS_UNMATCHED,
                    directory=DIR_UNMATCHED,
                    barcodes=values,
                    matched_barcode=None,
                    surname=None,
                    message="Распознанные штрихкоды отсутствуют в базе: " + ", ".join(values),
                )

            if len(unique_surnames) > 1:
                return self._finalize_with_copy(
                    doc_id=doc_id,
                    source=source,
                    source_info=info,
                    status=STATUS_NEEDS_REVIEW,
                    directory=DIR_NEEDS_REVIEW,
                    barcodes=values,
                    matched_barcode=None,
                    surname=None,
                    message=(
                        "Документ содержит штрихкоды разных фамилий ("
                        + ", ".join(unique_surnames)
                        + "); требуется ручная проверка"
                        + (f". {'; '.join(warnings)}" if warnings else "")
                    ),
                    error_note="Кандидаты: " + ", ".join(unique_surnames),
                )

            surname = unique_surnames[0]
            if surname.upper() in SERVICE_DIRECTORIES:
                return self._finalize_with_copy(
                    doc_id=doc_id,
                    source=source,
                    source_info=info,
                    status=STATUS_NEEDS_REVIEW,
                    directory=DIR_NEEDS_REVIEW,
                    barcodes=values,
                    matched_barcode=None,
                    surname=None,
                    message=(
                        f"Фамилия «{surname}» совпадает с названием служебной директории; "
                        "требуется ручная проверка"
                    ),
                    error_note=f"Недопустимая фамилия: {surname}",
                )
            matched_barcode = next(
                (barcode for barcode, value in matched.items() if sanitize_component(value) == surname),
                None,
            )
            return self._finalize_with_copy(
                doc_id=doc_id,
                source=source,
                source_info=info,
                status=STATUS_SORTED,
                directory=surname,
                barcodes=values,
                matched_barcode=matched_barcode,
                surname=surname,
                message=f"Документ отсортирован в папку «{surname}»"
                + (f". {'; '.join(warnings)}" if warnings else ""),
            )
        except ProcessingError as exc:
            return self._mark_error(doc_id, exc.message, retryable=exc.retryable)
        except OSError as exc:
            return self._mark_error(doc_id, f"Ошибка файловой системы: {exc}", retryable=True)
        except Exception as exc:
            logger.exception("Processing failed for document %s", info.relative_path)
            return self._mark_error(
                doc_id, f"Непредвиденная ошибка обработки: {exc}", retryable=True
            )

    # --------------------------------------------------------------- helpers
    def _hash_source(self, path: Path) -> SourceInfo:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ProcessingError(f"Не удалось открыть исходный файл: {exc}") from exc

        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "rb", closefd=True) as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ProcessingError("Исходный объект не является обычным файлом", retryable=False)
                total = 0
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                after = os.fstat(handle.fileno())
        except OSError as exc:
            raise ProcessingError(f"Ошибка чтения исходного файла: {exc}") from exc

        if (
            total != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise SourceChangedError()

        absolute = path.resolve(strict=True)
        try:
            relative = absolute.relative_to(self.input_root)
        except ValueError as exc:
            raise ProcessingError(
                "Исходный файл находится вне INPUT_DIR; обработка запрещена", retryable=False
            ) from exc
        if absolute == self.output_root or absolute.is_relative_to(self.output_root):
            raise ProcessingError(
                "Исходный файл находится в OUTPUT_DIR; обработка запрещена", retryable=False
            )
        return SourceInfo(
            relative_path=relative.as_posix(),
            absolute_path=absolute,
            sha256=digest.hexdigest(),
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
        )

    def _begin_document(self, info: SourceInfo) -> Document | None:
        """Create/update the processing record. Returns None when it must be skipped."""

        def _operation() -> Document | None:
            with self.db.session() as session:
                document = session.scalar(
                    select(Document).where(
                        Document.source_path == info.relative_path,
                        Document.sha256 == info.sha256,
                    )
                )
                if document is not None:
                    if document.status in TERMINAL_STATUSES:
                        return None
                    if document.status == STATUS_PROCESSING:
                        updated = document.updated_at
                        if updated is not None:
                            if updated.tzinfo is None:
                                updated = updated.replace(tzinfo=UTC)
                            age = (utc_now() - updated).total_seconds()
                            if age < self.settings.stale_processing_seconds:
                                return None
                    if (
                        document.status == STATUS_ERROR
                        and document.attempts >= self.settings.max_attempts
                    ):
                        return None
                    document.status = STATUS_PROCESSING
                    document.attempts += 1
                    document.size_bytes = info.size
                    document.source_mtime_ns = info.mtime_ns
                    document.error_message = None
                    document.updated_at = utc_now()
                    session.flush()
                    return document

                document = Document(
                    source_path=info.relative_path,
                    file_name=Path(info.relative_path).name,
                    sha256=info.sha256,
                    size_bytes=info.size,
                    source_mtime_ns=info.mtime_ns,
                    status=STATUS_PROCESSING,
                    attempts=1,
                )
                session.add(document)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    return None
                session.add(
                    ProcessingLog(
                        document_id=document.id,
                        level="INFO",
                        file_name=document.file_name,
                        source_path=document.source_path,
                        status=STATUS_PROCESSING,
                        message="Начата обработка документа",
                    )
                )
                return document

        return self.db.run_with_retry(_operation)

    def _lookup_mappings(self, values: list[str]) -> dict[str, str]:
        lookup_values = [value for value in values if len(value) <= 256]
        if not lookup_values:
            return {}

        def _operation() -> dict[str, str]:
            with self.db.session() as session:
                rows = session.scalars(
                    select(BarcodeMapping).where(BarcodeMapping.barcode.in_(lookup_values))
                ).all()
                return {row.barcode: row.surname for row in rows}

        return self.db.run_with_retry(_operation)

    def _normalize_barcodes(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            cleaned = str(value).strip("\x00").strip()
            if not cleaned:
                continue
            if len(cleaned) > MAX_BARCODE_LENGTH:
                cleaned = cleaned[:MAX_BARCODE_LENGTH]
            if cleaned not in result:
                result.append(cleaned)
            if len(result) >= MAX_BARCODES_PER_DOCUMENT:
                break
        return result

    def _finalize_with_copy(
        self,
        *,
        doc_id: int,
        source: Path,
        source_info: SourceInfo,
        status: str,
        directory: str,
        barcodes: list[str],
        matched_barcode: str | None,
        surname: str | None,
        message: str,
        error_note: str | None = None,
    ) -> ProcessOutcome:
        # Target path is committed to SQLite *before* the copy. If the process
        # crashes after the atomic publish but before the final commit, a retry
        # sees the recorded target, verifies its SHA-256 and reuses it instead
        # of creating a duplicate copy.  Existing files are never overwritten.
        target: Path | None = None
        for _ in range(10):
            existing_target = self._get_target(doc_id)
            if existing_target:
                existing_path = Path(existing_target)
                self.copier.validate_target(existing_path)
                if existing_path.is_symlink():
                    self._set_target(doc_id, None)
                elif existing_path.is_file():
                    if self._target_matches(existing_path, source_info):
                        target = existing_path
                        break
                    # A foreign file now occupies the reserved name. Preserve it
                    # and reserve a fresh name.
                    logger.warning(
                        "Reserved target %s was replaced externally; choosing a new name",
                        existing_path,
                    )
                    self._set_target(doc_id, None)
                elif not existing_path.exists():
                    try:
                        self.copier.copy_to_target(
                            source,
                            existing_path,
                            expected_size=source_info.size,
                            expected_mtime_ns=source_info.mtime_ns,
                        )
                        target = existing_path
                        break
                    except TargetExistsError:
                        self._set_target(doc_id, None)

            new_target = self.copier.choose_target(directory, source.name)
            self._set_target(doc_id, str(new_target))
            try:
                self.copier.copy_to_target(
                    source,
                    new_target,
                    expected_size=source_info.size,
                    expected_mtime_ns=source_info.mtime_ns,
                )
                target = new_target
                break
            except TargetExistsError:
                self._set_target(doc_id, None)
                continue

        if target is None:
            raise ProcessingError(
                "Не удалось зарезервировать уникальное имя выходного файла", retryable=True
            )

        self._store_success(
            doc_id=doc_id,
            status=status,
            barcodes=barcodes,
            matched_barcode=matched_barcode,
            surname=surname,
            target_path=str(target),
            message=message,
            error_note=error_note,
        )
        return ProcessOutcome(
            status=status,
            document_id=doc_id,
            barcodes=barcodes,
            matched_barcode=matched_barcode,
            surname=surname,
            target_path=str(target),
            error=error_note,
        )

    def _get_target(self, doc_id: int) -> str | None:
        def _operation() -> str | None:
            with self.db.session() as session:
                return session.scalar(select(Document.target_path).where(Document.id == doc_id))

        return self.db.run_with_retry(_operation)

    def _set_target(self, doc_id: int, target_path: str | None) -> None:
        def _operation() -> None:
            with self.db.session() as session:
                document = session.get(Document, doc_id)
                if document is None:
                    raise ProcessingError(
                        "Запись документа исчезла из базы данных", retryable=False
                    )
                document.target_path = target_path
                document.updated_at = utc_now()

        self.db.run_with_retry(_operation)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _target_matches(self, target: Path, source_info: SourceInfo) -> bool:
        try:
            if target.stat().st_size != source_info.size:
                return False
            return self._file_sha256(target) == source_info.sha256
        except OSError:
            return False

    def _store_success(
        self,
        *,
        doc_id: int,
        status: str,
        barcodes: list[str],
        matched_barcode: str | None,
        surname: str | None,
        target_path: str,
        message: str,
        error_note: str | None,
    ) -> None:
        def _operation() -> None:
            with self.db.session() as session:
                document = session.get(Document, doc_id)
                if document is None:
                    raise ProcessingError("Запись документа исчезла из базы данных", retryable=False)
                document.status = status
                document.barcodes = json.dumps(barcodes, ensure_ascii=False)
                document.matched_barcode = matched_barcode
                document.surname = surname
                document.target_path = target_path
                document.error_message = error_note
                document.processed_at = utc_now()
                document.next_retry_at = None
                document.updated_at = utc_now()
                session.add(
                    ProcessingLog(
                        document_id=doc_id,
                        level="WARNING" if status == STATUS_NEEDS_REVIEW else "INFO",
                        file_name=document.file_name,
                        source_path=document.source_path,
                        barcode=", ".join(barcodes)[:256] if barcodes else None,
                        status=status,
                        message=message,
                    )
                )

        self.db.run_with_retry(_operation)
        logger.info("Document %s processed with status %s", doc_id, status)

    def _mark_error(self, doc_id: int, message: str, *, retryable: bool) -> ProcessOutcome:
        retry_at: datetime | None = None

        def _operation() -> datetime | None:
            with self.db.session() as session:
                document = session.get(Document, doc_id)
                if document is None:
                    raise ProcessingError("Запись документа исчезла из базы данных", retryable=False)
                if not retryable:
                    document.attempts = self.settings.max_attempts
                    document.next_retry_at = None
                else:
                    delay = min(
                        self.settings.retry_base_seconds * (2 ** max(0, document.attempts - 1)),
                        self.settings.retry_max_seconds,
                    )
                    document.next_retry_at = utc_now() + timedelta(seconds=delay)
                document.status = STATUS_ERROR
                document.error_message = message
                document.updated_at = utc_now()
                session.add(
                    ProcessingLog(
                        document_id=doc_id,
                        level="ERROR",
                        file_name=document.file_name,
                        source_path=document.source_path,
                        status=STATUS_ERROR,
                        message=message,
                    )
                )
                return document.next_retry_at

        try:
            retry_at = self.db.run_with_retry(_operation)
        except Exception:
            logger.exception("Could not store error state for document %s", doc_id)
        logger.error("Document %s failed: %s", doc_id, message)
        return ProcessOutcome(
            status=STATUS_ERROR,
            document_id=doc_id,
            error=message,
            retry_at=retry_at,
        )
