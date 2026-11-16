"""CSV service tests."""

from __future__ import annotations

import csv
import io

import pytest

from app.database import Database
from app.models import BarcodeMapping
from app.services.csv_service import CsvImportError, export_csv, import_csv


def _add(db: Database, barcode: str, surname: str) -> None:
    with db.session() as session:
        session.add(BarcodeMapping(barcode=barcode, surname=surname))


def test_import_valid_csv_with_bom(db: Database) -> None:
    payload = "\ufeffbarcode,surname\r\n4601234567890,Иванов\r\n000123,Петров\r\n".encode("utf-8")
    report = import_csv(db, payload, max_rows=100)
    assert report.imported == 2
    assert report.skipped == 0
    assert report.error_count == 0
    with db.session() as session:
        rows = session.query(BarcodeMapping).order_by(BarcodeMapping.barcode).all()
        assert [(row.barcode, row.surname) for row in rows] == [
            ("000123", "Петров"),
            ("4601234567890", "Иванов"),
        ]


def test_import_skips_existing_and_does_not_change_it(db: Database) -> None:
    _add(db, "001", "Старый")
    payload = "barcode,surname\n001,Новый\n002,Другой\n".encode()
    report = import_csv(db, payload, max_rows=100)
    assert report.imported == 1
    assert report.skipped == 1
    with db.session() as session:
        old = session.query(BarcodeMapping).filter_by(barcode="001").one()
        assert old.surname == "Старый"


def test_import_duplicates_in_file_and_invalid_rows(db: Database) -> None:
    payload = (
        "barcode,surname\n"
        "001,Иванов\n"
        "001,Петров\n"
        "002,\n"
        ",Сидоров\n"
        "003,<script>\n"
        "004,Валидный\n"
    ).encode()
    report = import_csv(db, payload, max_rows=100)
    assert report.imported == 2
    assert report.skipped == 1
    assert report.error_count >= 3
    with db.session() as session:
        assert session.query(BarcodeMapping).count() == 2


def test_bad_header_is_rejected(db: Database) -> None:
    with pytest.raises(CsvImportError):
        import_csv(db, "surname,barcode\nИванов,1\n".encode(), max_rows=100)


def test_empty_file_is_rejected(db: Database) -> None:
    with pytest.raises(CsvImportError):
        import_csv(db, b"", max_rows=100)


def test_too_many_rows_is_rejected(db: Database) -> None:
    lines = ["barcode,surname"] + [f"{i},Фамилия{i}" for i in range(10)]
    with pytest.raises(CsvImportError):
        import_csv(db, "\n".join(lines).encode(), max_rows=5)


def test_cp1251_is_supported(db: Database) -> None:
    payload = "barcode,surname\n123,Иванов\n".encode("cp1251")
    report = import_csv(db, payload, max_rows=100)
    assert report.imported == 1


def test_export_has_bom_header_and_roundtrips(db: Database) -> None:
    _add(db, "001", "Иванов")
    _add(db, "002", "Петров")
    payload = export_csv(db)
    assert payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["barcode", "surname"]
    assert ["001", "Иванов"] in rows
    assert ["002", "Петров"] in rows
