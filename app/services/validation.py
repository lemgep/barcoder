"""Shared validation for barcodes."""

from __future__ import annotations

MAX_BARCODE_LENGTH = 256


def validate_barcode(value: str) -> str:
    barcode = str(value or "").strip()
    if not barcode:
        raise ValueError("Штрихкод не может быть пустым")
    if len(barcode) > MAX_BARCODE_LENGTH:
        raise ValueError(f"Штрихкод длиннее {MAX_BARCODE_LENGTH} символов")
    if any(ord(char) < 32 or ord(char) == 127 for char in barcode):
        raise ValueError("Штрихкод содержит управляющие символы")
    return barcode
