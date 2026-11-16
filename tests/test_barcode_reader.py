"""Barcode recognition tests (Code 128, QR, PDF, images, errors)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.services.barcode_reader import (
    BarcodeConfigError,
    BarcodeReader,
    parse_barcode_formats,
)
from app.services.errors import DamagedDocumentError, EncryptedDocumentError
from tests.helpers import (
    ean13_with_check_digit,
    save_barcode,
    save_encrypted_pdf,
    save_multi_barcode_image,
    save_pdf_with_barcode,
    save_pdf_without_barcode,
)


def test_code128_recognition(reader: BarcodeReader, tmp_path: Path) -> None:
    path = save_barcode(tmp_path / "code128.png", "ABC-12345")
    result = reader.extract(path)
    assert [item.text for item in result.detections] == ["ABC-12345"]
    assert result.detections[0].format_name == "Code 128"


def test_qr_recognition(reader: BarcodeReader, tmp_path: Path) -> None:
    path = save_barcode(tmp_path / "qr.png", "QR-PAYLOAD-42", "qr")
    result = reader.extract(path)
    assert result.unique_values == ["QR-PAYLOAD-42"]


def test_all_required_linear_and_matrix_formats(reader: BarcodeReader, tmp_path: Path) -> None:
    ean13 = ean13_with_check_digit("460123456789")
    cases = [
        ("code39", "CODE39-TEST", "CODE39-TEST"),
        ("datamatrix", "DM-001", "DM-001"),
        ("pdf417", "PDF417-1", "PDF417-1"),
        ("ean13", "460123456789", ean13),
        ("ean8", "1234567", "12345670"),
        ("itf", "12345678", "12345678"),
    ]
    for kind, value, expected in cases:
        path = save_barcode(tmp_path / f"{kind}.png", value, kind)
        result = reader.extract(path)
        assert result.detections, f"{kind} was not recognized"
        assert result.detections[0].text == expected


def test_barcode_in_multipage_pdf_on_second_page(reader: BarcodeReader, tmp_path: Path) -> None:
    path = save_pdf_with_barcode(
        tmp_path / "document.pdf", "PDF-000123", page_count=3, barcode_page=2
    )
    result = reader.extract(path)
    assert result.pages_processed == 3
    assert result.unique_values == ["PDF-000123"]
    assert result.detections[0].page == 2


def test_barcode_in_jpeg_image(reader: BarcodeReader, tmp_path: Path) -> None:
    path = tmp_path / "scan.jpg"
    image = Image.open(save_barcode(tmp_path / "source.png", "IMG-777"))
    image.convert("RGB").save(path, quality=92)
    result = reader.extract(path)
    assert result.unique_values == ["IMG-777"]


def test_multipage_tiff_and_rotation(reader: BarcodeReader, tmp_path: Path) -> None:
    first = Image.open(save_barcode(tmp_path / "tiff_source.png", "TIFF-PAGE-2")).convert("L")
    blank = Image.new("L", first.size, "white")
    rotated = first.rotate(90, expand=True)
    path = tmp_path / "multipage.tif"
    blank.save(path, save_all=True, append_images=[rotated], format="TIFF")

    result = reader.extract(path)
    assert result.pages_processed == 2
    assert "TIFF-PAGE-2" in result.unique_values


def test_pdf_page_limit_produces_warning(settings, tmp_path: Path) -> None:
    settings.max_pdf_pages = 1
    reader = BarcodeReader(settings)
    path = save_pdf_with_barcode(
        tmp_path / "limited.pdf", "LAST-PAGE-ONLY", page_count=3, barcode_page=3
    )
    result = reader.extract(path)
    assert result.pages_processed == 1
    assert result.unique_values == []
    assert any("MAX_PDF_PAGES" in warning for warning in result.warnings)


def test_multiple_barcodes_on_one_image(reader: BarcodeReader, tmp_path: Path) -> None:
    path = save_multi_barcode_image(
        tmp_path / "multi.png", [("AAA-1", "code128"), ("BBB-2", "qr")]
    )
    result = reader.extract(path)
    assert set(result.unique_values) == {"AAA-1", "BBB-2"}


def test_pdf_without_barcode_returns_empty(reader: BarcodeReader, tmp_path: Path) -> None:
    result = reader.extract(save_pdf_without_barcode(tmp_path / "empty.pdf"))
    assert result.detections == []
    assert result.pages_processed == 1


def test_encrypted_pdf_is_rejected(reader: BarcodeReader, tmp_path: Path) -> None:
    with pytest.raises(EncryptedDocumentError):
        reader.extract(save_encrypted_pdf(tmp_path / "encrypted.pdf"))


def test_damaged_pdf_is_rejected(reader: BarcodeReader, tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(DamagedDocumentError):
        reader.extract(path)


def test_unknown_extension_is_rejected(reader: BarcodeReader, tmp_path: Path) -> None:
    path = tmp_path / "document.docx"
    path.write_bytes(b"not a scan")
    from app.services.errors import UnsupportedDocumentError

    with pytest.raises(UnsupportedDocumentError):
        reader.extract(path)


def test_invalid_format_configuration_fails_loudly() -> None:
    with pytest.raises(BarcodeConfigError):
        parse_barcode_formats("Code128,NotAFormat")


def test_formats_can_be_extended(reader: BarcodeReader) -> None:
    # Includes required formats and extra ones; parser de-duplicates.
    assert parse_barcode_formats("Code128, Code 128, QRCode, Aztec") is not None
