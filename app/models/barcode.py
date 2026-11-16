"""Barcode -> surname mapping."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.processing import utc_now


class BarcodeMapping(Base):
    __tablename__ = "barcode_mappings"
    __table_args__ = (
        CheckConstraint("length(barcode) > 0", name="ck_barcode_not_empty"),
        CheckConstraint("length(surname) > 0", name="ck_surname_not_empty"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    barcode: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    surname: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<BarcodeMapping {self.barcode!r} -> {self.surname!r}>"
