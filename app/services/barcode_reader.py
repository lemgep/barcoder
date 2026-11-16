"""Barcode extraction from PDF and raster images using ZXing-C++."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import zxingcpp
from PIL import Image, ImageFilter, ImageOps, ImageSequence

from app.config import Settings
from app.services.errors import (
    DamagedDocumentError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    UnsupportedDocumentError,
)

logger = logging.getLogger(__name__)

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
SUPPORTED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES

# Human-friendly names used in configuration and in the UI.
_FORMAT_NAMES = {
    "CODE128": zxingcpp.BarcodeFormat.Code128,
    "CODE39": zxingcpp.BarcodeFormat.Code39,
    "CODE93": zxingcpp.BarcodeFormat.Code93,
    "EAN13": zxingcpp.BarcodeFormat.EAN13,
    "EAN8": zxingcpp.BarcodeFormat.EAN8,
    "UPCA": zxingcpp.BarcodeFormat.UPCA,
    "UPCE": zxingcpp.BarcodeFormat.UPCE,
    "QRCODE": zxingcpp.BarcodeFormat.QRCode,
    "QR": zxingcpp.BarcodeFormat.QRCode,
    "DATA MATRIX": zxingcpp.BarcodeFormat.DataMatrix,
    "DATAMATRIX": zxingcpp.BarcodeFormat.DataMatrix,
    "PDF417": zxingcpp.BarcodeFormat.PDF417,
    "ITF": zxingcpp.BarcodeFormat.ITF,
    "CODABAR": zxingcpp.BarcodeFormat.Codabar,
    "AZTEC": zxingcpp.BarcodeFormat.Aztec,
}

_FORMAT_LABELS = {
    zxingcpp.BarcodeFormat.Code128: "Code 128",
    zxingcpp.BarcodeFormat.Code39: "Code 39",
    zxingcpp.BarcodeFormat.Code93: "Code 93",
    zxingcpp.BarcodeFormat.EAN13: "EAN-13",
    zxingcpp.BarcodeFormat.EAN8: "EAN-8",
    zxingcpp.BarcodeFormat.UPCA: "UPC-A",
    zxingcpp.BarcodeFormat.UPCE: "UPC-E",
    zxingcpp.BarcodeFormat.QRCode: "QR Code",
    zxingcpp.BarcodeFormat.DataMatrix: "Data Matrix",
    zxingcpp.BarcodeFormat.PDF417: "PDF417",
    zxingcpp.BarcodeFormat.ITF: "ITF",
    zxingcpp.BarcodeFormat.Codabar: "Codabar",
    zxingcpp.BarcodeFormat.Aztec: "Aztec",
}


class BarcodeConfigError(ValueError):
    """Invalid BARCODE_FORMATS configuration."""


def parse_barcode_formats(raw: str) -> zxingcpp.BarcodeFormats:
    """Parse a comma-separated list of format names.

    Kept deliberately explicit so typos in configuration fail loudly instead of
    silently disabling recognition of a format.
    """

    selected: list[zxingcpp.BarcodeFormat] = []
    unknown: list[str] = []
    for item in raw.split(","):
        name = " ".join(item.strip().upper().replace("-", " ").split())
        if not name:
            continue
        # Accept both "Code 128" and "Code128".
        fmt = _FORMAT_NAMES.get(name) or _FORMAT_NAMES.get(name.replace(" ", ""))
        if fmt is None:
            unknown.append(item.strip())
        elif fmt not in selected:
            selected.append(fmt)
    if unknown:
        raise BarcodeConfigError(
            "Неизвестные форматы штрихкодов в BARCODE_FORMATS: " + ", ".join(unknown)
        )
    if not selected:
        raise BarcodeConfigError("BARCODE_FORMATS не содержит ни одного формата")
    return zxingcpp.BarcodeFormats(selected)


@dataclass(frozen=True)
class Detection:
    text: str
    format_name: str
    page: int
    source_name: str


@dataclass
class ExtractionResult:
    detections: list[Detection] = field(default_factory=list)
    pages_processed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def unique_values(self) -> list[str]:
        seen: set[str] = set()
        values: list[str] = []
        for detection in self.detections:
            if detection.text not in seen:
                seen.add(detection.text)
                values.append(detection.text)
        return values


class BarcodeReader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.formats = parse_barcode_formats(settings.barcode_formats)

    def extract(self, path: Path, deadline: float | None = None) -> ExtractionResult:
        """Extract barcodes from one document.

        ``deadline`` is a ``time.monotonic()`` value.  Processing never runs
        unbounded: each page has a budget check and we stop when it is exceeded.
        """

        if deadline is None:
            deadline = time.monotonic() + self.settings.max_document_seconds

        suffix = path.suffix.lower()
        if suffix in PDF_SUFFIXES:
            return self._extract_pdf(path, deadline)
        if suffix in IMAGE_SUFFIXES:
            return self._extract_image(path, deadline)
        raise UnsupportedDocumentError(f"Неподдерживаемый формат файла: {suffix or '(без расширения)'}")

    # ------------------------------------------------------------------ PDF
    def _extract_pdf(self, path: Path, deadline: float) -> ExtractionResult:
        result = ExtractionResult()
        try:
            document = pymupdf.open(path)  # opens read-only
        except Exception as exc:
            raise DamagedDocumentError(f"Не удалось открыть PDF: {exc}") from exc

        try:
            if document.needs_pass or document.is_encrypted:
                raise EncryptedDocumentError("PDF защищён паролем и не может быть прочитан")
            page_count = document.page_count
            if page_count <= 0:
                raise DamagedDocumentError("PDF не содержит страниц")
            pages_to_read = min(page_count, self.settings.max_pdf_pages)
            if page_count > pages_to_read:
                result.warnings.append(
                    f"PDF содержит {page_count} страниц; обработаны первые {pages_to_read} "
                    f"(ограничение MAX_PDF_PAGES)"
                )

            for index in range(pages_to_read):
                if time.monotonic() > deadline:
                    result.warnings.append(
                        "Превышен лимит времени обработки документа; оставшиеся страницы пропущены"
                    )
                    break
                page = document.load_page(index)
                image = self._render_page(page)
                result.pages_processed += 1
                self._decode_into(result, image, page=index + 1, source_name=path.name, deadline=deadline)
                image.close()
            return result
        except (EncryptedDocumentError, DamagedDocumentError):
            raise
        except Exception as exc:
            raise DamagedDocumentError(f"Ошибка чтения PDF: {exc}") from exc
        finally:
            document.close()

    def _render_page(self, page: pymupdf.Page) -> Image.Image:
        dpi = self.settings.pdf_dpi
        # Prevent a huge page from exhausting memory/disk at high DPI.
        rect = page.rect
        estimated_pixels = max(1.0, abs(rect.width * rect.height) * (dpi / 72.0) ** 2)
        if estimated_pixels > self.settings.max_image_pixels:
            import math

            scale = math.sqrt(self.settings.max_image_pixels / estimated_pixels)
            dpi = max(72, int(dpi * scale))
        pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        return Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)

    # -------------------------------------------------------------- images
    def _extract_image(self, path: Path, deadline: float) -> ExtractionResult:
        result = ExtractionResult()
        try:
            with Image.open(path) as image:
                for frame_index, frame in enumerate(ImageSequence.Iterator(image)):
                    if time.monotonic() > deadline:
                        result.warnings.append(
                            "Превышен лимит времени обработки документа; оставшиеся кадры пропущены"
                        )
                        break
                    result.pages_processed += 1
                    frame = ImageOps.exif_transpose(frame)
                    prepared = self._prepare_image(frame)
                    try:
                        self._decode_into(
                            result,
                            prepared,
                            page=frame_index + 1,
                            source_name=path.name,
                            deadline=deadline,
                        )
                    finally:
                        prepared.close()
        except (Image.DecompressionBombError, MemoryError) as exc:
            raise DocumentTooLargeError("Изображение слишком большое для безопасной обработки") from exc
        except OSError as exc:
            raise DamagedDocumentError(f"Не удалось прочитать изображение: {exc}") from exc
        except ValueError as exc:
            raise DamagedDocumentError(f"Некорректное изображение: {exc}") from exc
        return result

    def _prepare_image(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise DamagedDocumentError("Изображение имеет нулевой размер")
        if width * height > self.settings.max_image_pixels:
            import math

            scale = math.sqrt(self.settings.max_image_pixels / (width * height))
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )
        prepared = image.convert("L")
        prepared.load()
        return prepared

    # -------------------------------------------------------------- decode
    def _decode_into(
        self,
        result: ExtractionResult,
        image: Image.Image,
        *,
        page: int,
        source_name: str,
        deadline: float,
    ) -> None:
        """Decode one page/frame, trying a small set of preprocessing variants."""

        found_before = len(result.detections)
        self._decode_variant(result, image, page, source_name)

        if len(result.detections) == found_before and time.monotonic() <= deadline:
            # Mild enhancement helps with faded scans and low contrast.
            enhanced = ImageOps.autocontrast(image)
            try:
                self._decode_variant(result, enhanced, page, source_name)
            finally:
                enhanced.close()

        if len(result.detections) == found_before and time.monotonic() <= deadline:
            # Sharpen slightly, then try an aggressive binarization.
            sharpened = image.filter(ImageFilter.SHARPEN)
            try:
                sharpened = ImageOps.autocontrast(sharpened)
                self._decode_variant(result, sharpened, page, source_name)
            finally:
                sharpened.close()

        if len(result.detections) == found_before and time.monotonic() <= deadline:
            thresholded = image.point(lambda value: 255 if value > 150 else 0)
            try:
                self._decode_variant(result, thresholded, page, source_name)
            finally:
                thresholded.close()

        # Small barcodes (e.g. a QR in a corner of an A4 scan) benefit from 2x.
        width, height = image.size
        if (
            len(result.detections) == found_before
            and max(width, height) < 2200
            and time.monotonic() <= deadline
        ):
            upscaled = image.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
            try:
                self._decode_variant(result, upscaled, page, source_name)
            finally:
                upscaled.close()

    def _decode_variant(
        self,
        result: ExtractionResult,
        image: Image.Image,
        page: int,
        source_name: str,
    ) -> None:
        try:
            barcodes = zxingcpp.read_barcodes(
                image,
                formats=self.formats,
                try_rotate=True,
                try_downscale=True,
            )
        except Exception:
            logger.warning("ZXing failed on %s page %s", source_name, page, exc_info=True)
            return

        for barcode in barcodes:
            text = barcode.text or ""
            # NUL bytes and surrounding whitespace are artifacts, not data.
            text = text.strip("\x00").strip()
            if not text:
                continue
            format_name = _FORMAT_LABELS.get(barcode.format, str(barcode.format))
            detection = Detection(
                text=text,
                format_name=format_name,
                page=page,
                source_name=source_name,
            )
            if detection not in result.detections:
                result.detections.append(detection)

    @staticmethod
    def describe(detections: list[Detection]) -> str:
        if not detections:
            return ""
        return ", ".join(
            f"{item.text} ({item.format_name}, стр. {item.page})" for item in detections
        )
