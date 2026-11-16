"""CSV import/export for barcode -> surname mappings."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.database import Database
from app.models import BarcodeMapping
from app.services.file_copier import sanitize_surname
from app.services.validation import validate_barcode

logger = logging.getLogger(__name__)


class CsvImportError(Exception):
    pass


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvImportError(
        "Не удалось определить кодировку CSV. Используйте UTF-8 (рекомендуется) или Windows-1251."
    )


def import_csv(
    database: Database,
    data: bytes,
    *,
    max_rows: int,
) -> ImportReport:
    """Parse and import a CSV file.

    Validation happens before any INSERT, and all inserts are committed in one
    transaction.  A failure leaves existing mappings untouched.
    """

    text = _decode(data)
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream)
    report = ImportReport()

    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvImportError("CSV-файл пуст") from exc
    except csv.Error as exc:
        raise CsvImportError(f"Некорректный CSV: {exc}") from exc

    normalized_header = [cell.strip().lower() for cell in header]
    if normalized_header[:2] != ["barcode", "surname"] or len(normalized_header) != 2:
        raise CsvImportError("Ожидается заголовок CSV: barcode,surname")

    valid_rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    row_count = 0

    try:
        for line_number, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            row_count += 1
            if row_count > max_rows:
                raise CsvImportError(f"CSV содержит больше {max_rows} строк — импорт отклонён")
            if len(row) != 2:
                report.errors.append(f"Строка {line_number}: ожидается 2 колонки (barcode,surname)")
                continue
            try:
                barcode = validate_barcode(row[0])
                surname = sanitize_surname(row[1])
            except ValueError as exc:
                report.errors.append(f"Строка {line_number}: {exc}")
                continue
            if barcode in seen:
                report.skipped += 1
                report.errors.append(f"Строка {line_number}: дубликат штрихкода в файле")
                continue
            seen.add(barcode)
            valid_rows.append((barcode, surname))
    except csv.Error as exc:
        raise CsvImportError(f"Некорректный CSV: {exc}") from exc

    if not valid_rows:
        return report

    def _operation() -> int:
        imported = 0
        with database.session() as session:
            existing: set[str] = set()
            barcodes = [barcode for barcode, _ in valid_rows]
            for offset in range(0, len(barcodes), 400):
                chunk = barcodes[offset : offset + 400]
                existing.update(
                    session.scalars(
                        select(BarcodeMapping.barcode).where(BarcodeMapping.barcode.in_(chunk))
                    ).all()
                )
            for barcode, surname in valid_rows:
                if barcode in existing:
                    report.skipped += 1
                    continue
                session.add(BarcodeMapping(barcode=barcode, surname=surname))
                existing.add(barcode)
                imported += 1
            session.flush()
        return imported

    report.imported = database.run_with_retry(_operation)
    # Keep the UI report compact for very broken files.
    if len(report.errors) > 100:
        extra = len(report.errors) - 100
        report.errors = [*report.errors[:100], f"... и ещё {extra} ошибок"]
    logger.info(
        "CSV import finished: imported=%s skipped=%s errors=%s",
        report.imported,
        report.skipped,
        report.error_count,
    )
    return report


def export_csv(database: Database) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["barcode", "surname"])

    def _operation() -> list[tuple[str, str]]:
        with database.session() as session:
            rows = session.execute(
                select(BarcodeMapping.barcode, BarcodeMapping.surname).order_by(
                    BarcodeMapping.surname, BarcodeMapping.barcode
                )
            ).all()
            return [(row.barcode, row.surname) for row in rows]

    for barcode, surname in database.run_with_retry(_operation):
        writer.writerow([barcode, surname])
    # BOM keeps Cyrillic readable in Excel.
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")
