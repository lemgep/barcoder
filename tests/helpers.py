"""Test helpers: generate barcodes without any external services."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pymupdf
import zxingcpp
from PIL import Image


def snapshot_tree(root: Path) -> dict[str, tuple]:
    """Capture names, metadata and content hashes for an immutability check."""

    snapshot: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_file():
            snapshot[relative] = (
                "file",
                info.st_size,
                info.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        else:
            snapshot[relative] = ("dir", info.st_mode, info.st_mtime_ns)
    return snapshot


def ean13_with_check_digit(first_12: str) -> str:
    digits = [int(char) for char in first_12]
    checksum = (10 - (sum(digits[::2]) + 3 * sum(digits[1::2])) % 10) % 10
    return first_12 + str(checksum)


BARCODE_FORMATS = {
    "code128": zxingcpp.BarcodeFormat.Code128,
    "code39": zxingcpp.BarcodeFormat.Code39,
    "qr": zxingcpp.BarcodeFormat.QRCode,
    "datamatrix": zxingcpp.BarcodeFormat.DataMatrix,
    "pdf417": zxingcpp.BarcodeFormat.PDF417,
    "ean13": zxingcpp.BarcodeFormat.EAN13,
    "ean8": zxingcpp.BarcodeFormat.EAN8,
    "itf": zxingcpp.BarcodeFormat.ITF,
}


def barcode_image(text: str, kind: str = "code128", *, scale: int = 1) -> Image.Image:
    barcode = zxingcpp.create_barcode(text, BARCODE_FORMATS[kind])
    image = zxingcpp.write_barcode_to_image(
        barcode, scale=max(1, scale * 2), add_quiet_zones=True
    )
    height, width = image.shape
    pil = Image.frombytes("L", (width, height), bytes(image))
    return pil.convert("RGB")


def save_barcode(path: Path, text: str, kind: str = "code128", *, scale: int = 1) -> Path:
    if kind in {"ean8", "ean13"}:
        # zxingcpp's encoder normalises EAN-8 to EAN-13, so use an independent
        # encoder for these tests.
        from barcode import EAN8, EAN13
        from barcode.writer import ImageWriter

        encoder = EAN8 if kind == "ean8" else EAN13
        stem = path.with_suffix("")
        produced = Path(encoder(text, writer=ImageWriter()).save(str(stem)))
        return produced
    image = barcode_image(text, kind, scale=scale)
    image.save(path)
    return path


def save_multi_barcode_image(
    path: Path, values: list[tuple[str, str]], *, spacing: int = 40
) -> Path:
    images = [barcode_image(value, kind) for value, kind in values]
    width = max(image.width for image in images)
    height = sum(image.height for image in images) + spacing * (len(images) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for image in images:
        canvas.paste(image, (0, y))
        y += image.height + spacing
    canvas.save(path)
    return path


def save_pdf_with_barcode(
    path: Path,
    text: str,
    *,
    kind: str = "code128",
    page_count: int = 1,
    barcode_page: int = 1,
) -> Path:
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page(width=595, height=842)  # A4 points
        page.insert_text((72, 72), f"Test document, page {index + 1}")
        if index + 1 == barcode_page:
            buffer = io.BytesIO()
            barcode_image(text, kind).save(buffer, format="PNG")
            page.insert_image(pymupdf.Rect(72, 120, 523, 260), stream=buffer.getvalue())
            page.insert_text((72, 300), f"Barcode: {text}")
    document.save(path)
    document.close()
    return path


def save_pdf_without_barcode(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "No barcode here")
    page.draw_rect(pymupdf.Rect(72, 120, 523, 260), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    document.save(path)
    document.close()
    return path


def save_encrypted_pdf(path: Path, password: str = "secret") -> Path:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Encrypted")
    document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password)
    document.close()
    return path
