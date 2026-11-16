"""Processing log viewer with filtering and pagination."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from app.deps import get_database, get_templates, require_user
from app.models import ALL_STATUSES, STATUS_LABELS, ProcessingLog
from app.security import verify_csrf

router = APIRouter(
    prefix="/logs",
    dependencies=[Depends(require_user), Depends(verify_csrf)],
)

PAGE_SIZE = 50


@router.get("", name="logs_list")
def list_logs(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    page: int = Query(1, ge=1),
):
    database = get_database(request)
    templates = get_templates(request)
    q = q.strip()[:200]
    if status and status not in ALL_STATUSES:
        status = ""

    def _operation():
        with database.session() as session:
            statement = select(ProcessingLog)
            if status:
                statement = statement.where(ProcessingLog.status == status)
            if q:
                statement = statement.where(
                    or_(
                        ProcessingLog.file_name.contains(q, autoescape=True),
                        ProcessingLog.source_path.contains(q, autoescape=True),
                        ProcessingLog.barcode.contains(q, autoescape=True),
                        ProcessingLog.message.contains(q, autoescape=True),
                    )
                )
            total = session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            ) or 0
            pages = max(1, math.ceil(total / PAGE_SIZE))
            current = min(max(page, 1), pages)
            rows = list(
                session.scalars(
                    statement.order_by(ProcessingLog.created_at.desc(), ProcessingLog.id.desc())
                    .offset((current - 1) * PAGE_SIZE)
                    .limit(PAGE_SIZE)
                ).all()
            )
            return rows, total, current, pages

    rows, total, current, pages = database.run_with_retry(_operation)
    distinct_statuses = [
        (value, STATUS_LABELS.get(value, value))
        for value in sorted(ALL_STATUSES)
    ]
    return templates.render(
        request,
        "logs.html",
        items=rows,
        total=total,
        page=current,
        pages=pages,
        q=q,
        status=status,
        statuses=distinct_statuses,
    )
