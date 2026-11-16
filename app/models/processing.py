"""Processing state and audit log."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SORTED = "sorted"
STATUS_UNMATCHED = "unmatched"
STATUS_UNREADABLE = "unreadable"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_ERROR = "error"

TERMINAL_STATUSES = frozenset(
    {STATUS_SORTED, STATUS_UNMATCHED, STATUS_UNREADABLE, STATUS_NEEDS_REVIEW}
)
ALL_STATUSES = TERMINAL_STATUSES | {STATUS_PENDING, STATUS_PROCESSING, STATUS_ERROR}

STATUS_LABELS = {
    STATUS_PENDING: "В очереди",
    STATUS_PROCESSING: "Обрабатывается",
    STATUS_SORTED: "Отсортирован",
    STATUS_UNMATCHED: "Не найдено соответствие",
    STATUS_UNREADABLE: "Штрихкод не распознан",
    STATUS_NEEDS_REVIEW: "Требует проверки",
    STATUS_ERROR: "Ошибка обработки",
}

STATUS_CSS = {
    STATUS_PENDING: "secondary",
    STATUS_PROCESSING: "info",
    STATUS_SORTED: "success",
    STATUS_UNMATCHED: "warning",
    STATUS_UNREADABLE: "dark",
    STATUS_NEEDS_REVIEW: "primary",
    STATUS_ERROR: "danger",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_path", "sha256", name="uq_document_source_sha"),
        Index("ix_documents_source_path_id", "source_path", "id"),
        Index("ix_documents_status_retry", "status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_mtime_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STATUS_PENDING, index=True
    )
    barcodes: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_barcode: Mapped[str | None] = mapped_column(String(256), nullable=True)
    surname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    logs: Mapped[list[ProcessingLog]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def status_css(self) -> str:
        return STATUS_CSS.get(self.status, "secondary")

    @property
    def barcode_values(self) -> list[str]:
        if not self.barcodes:
            return []
        try:
            value = json.loads(self.barcodes)
        except (TypeError, ValueError):
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        return []


class ProcessingLog(Base):
    __tablename__ = "processing_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped[Document | None] = relationship(back_populates="logs")

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status or "", self.status or "")
