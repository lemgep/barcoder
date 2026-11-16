"""Dashboard statistics helpers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from app.database import Database
from app.models import (
    STATUS_ERROR,
    STATUS_NEEDS_REVIEW,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SORTED,
    STATUS_UNMATCHED,
    STATUS_UNREADABLE,
    Document,
)


@dataclass
class DashboardStats:
    total: int = 0
    sorted: int = 0
    unmatched: int = 0
    unreadable: int = 0
    needs_review: int = 0
    errors: int = 0
    queued: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "sorted": self.sorted,
            "unmatched": self.unmatched,
            "unreadable": self.unreadable,
            "needs_review": self.needs_review,
            "errors": self.errors,
            "queued": self.queued,
        }


def get_stats(database: Database) -> DashboardStats:
    def _operation() -> dict[str, int]:
        with database.session() as session:
            rows = session.execute(
                select(Document.status, func.count(Document.id)).group_by(Document.status)
            ).all()
            return {status: count for status, count in rows}

    counts = database.run_with_retry(_operation)
    stats = DashboardStats()
    stats.total = sum(counts.values())
    stats.sorted = counts.get(STATUS_SORTED, 0)
    stats.unmatched = counts.get(STATUS_UNMATCHED, 0)
    stats.unreadable = counts.get(STATUS_UNREADABLE, 0)
    stats.needs_review = counts.get(STATUS_NEEDS_REVIEW, 0)
    stats.errors = counts.get(STATUS_ERROR, 0)
    stats.queued = counts.get(STATUS_PENDING, 0) + counts.get(STATUS_PROCESSING, 0)
    return stats


def get_recent_documents(database: Database, limit: int = 20) -> list[Document]:
    def _operation() -> list[Document]:
        with database.session() as session:
            return list(
                session.scalars(
                    select(Document)
                    .order_by(Document.updated_at.desc(), Document.id.desc())
                    .limit(limit)
                ).all()
            )

    return database.run_with_retry(_operation)
