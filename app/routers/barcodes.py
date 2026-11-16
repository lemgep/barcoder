"""Barcode mapping CRUD, search and CSV import/export."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.deps import get_database, get_settings, get_templates, require_user
from app.models import BarcodeMapping
from app.security import verify_csrf
from app.services.csv_service import CsvImportError, export_csv, import_csv
from app.services.file_copier import sanitize_surname
from app.services.validation import validate_barcode
from app.templating import set_flash

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/barcodes",
    dependencies=[Depends(require_user), Depends(verify_csrf)],
)

PAGE_SIZE = 25


def _list_context(
    request: Request,
    *,
    q: str,
    field: str,
    page: int,
):
    database = get_database(request)
    q = q.strip()[:200]
    if field not in {"all", "barcode", "surname"}:
        field = "all"

    def _operation():
        with database.session() as session:
            statement = select(BarcodeMapping)
            if q:
                if field == "barcode":
                    statement = statement.where(BarcodeMapping.barcode.contains(q, autoescape=True))
                elif field == "surname":
                    statement = statement.where(BarcodeMapping.surname.contains(q, autoescape=True))
                else:
                    statement = statement.where(
                        or_(
                            BarcodeMapping.barcode.contains(q, autoescape=True),
                            BarcodeMapping.surname.contains(q, autoescape=True),
                        )
                    )
            total = session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            ) or 0
            pages = max(1, math.ceil(total / PAGE_SIZE))
            current = min(max(page, 1), pages)
            rows = list(
                session.scalars(
                    statement.order_by(BarcodeMapping.surname, BarcodeMapping.barcode)
                    .offset((current - 1) * PAGE_SIZE)
                    .limit(PAGE_SIZE)
                ).all()
            )
            return rows, total, current, pages

    rows, total, current, pages = database.run_with_retry(_operation)
    return {
        "items": rows,
        "total": total,
        "page": current,
        "pages": pages,
        "q": q,
        "field": field,
    }


@router.get("", name="barcodes_list")
def list_barcodes(
    request: Request,
    q: str = Query(""),
    field: str = Query("all"),
    page: int = Query(1, ge=1),
):
    templates = get_templates(request)
    return templates.render(
        request,
        "barcodes/list.html",
        **_list_context(request, q=q, field=field, page=page),
    )


@router.post("", name="barcodes_add")
def add_barcode(
    request: Request,
    barcode: str = Form(""),
    surname: str = Form(""),
):
    database = get_database(request)
    templates = get_templates(request)
    try:
        barcode_value = validate_barcode(barcode)
        surname_value = sanitize_surname(surname)
    except ValueError as exc:
        return templates.render(
            request,
            "barcodes/list.html",
            status_code=400,
            error=str(exc),
            add_barcode=barcode,
            add_surname=surname,
            **_list_context(request, q="", field="all", page=1),
        )

    def _operation() -> str:
        with database.session() as session:
            existing = session.scalar(
                select(BarcodeMapping.id).where(BarcodeMapping.barcode == barcode_value)
            )
            if existing is not None:
                return "duplicate"
            session.add(BarcodeMapping(barcode=barcode_value, surname=surname_value))
            return "created"

    try:
        result = database.run_with_retry(_operation)
    except IntegrityError:
        result = "duplicate"
    if result == "duplicate":
        set_flash(request, "warning", f"Штрихкод {barcode_value} уже существует")
    else:
        set_flash(request, "success", f"Добавлено: {barcode_value} → {surname_value}")
    return RedirectResponse("/barcodes", status_code=303)


@router.get("/export.csv", name="barcodes_export")
def export_barcodes(request: Request):
    database = get_database(request)
    payload = export_csv(database)
    filename = "barcodes-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + ".csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", name="barcodes_import")
async def import_barcodes(request: Request, file: UploadFile = File(...)):
    settings = get_settings(request)
    templates = get_templates(request)
    if not file.filename:
        return templates.render(
            request,
            "barcodes/import_result.html",
            status_code=400,
            fatal_error="Файл не выбран",
            report=None,
            filename=None,
        )
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        return templates.render(
            request,
            "barcodes/import_result.html",
            status_code=413,
            fatal_error=f"Файл больше допустимого размера ({settings.max_upload_bytes} байт)",
            report=None,
            filename=file.filename,
        )
    try:
        report = import_csv(get_database(request), data, max_rows=settings.max_csv_rows)
    except CsvImportError as exc:
        return templates.render(
            request,
            "barcodes/import_result.html",
            status_code=400,
            fatal_error=str(exc),
            report=None,
            filename=file.filename,
        )
    except Exception as exc:
        logger.exception("CSV import failed")
        return templates.render(
            request,
            "barcodes/import_result.html",
            status_code=500,
            fatal_error=f"Импорт не выполнен: {exc}",
            report=None,
            filename=file.filename,
        )
    set_flash(
        request,
        "success" if report.imported else "info",
        f"Импорт CSV: добавлено {report.imported}, пропущено {report.skipped}, "
        f"ошибок {report.error_count}",
    )
    return templates.render(
        request,
        "barcodes/import_result.html",
        fatal_error=None,
        report=report,
        filename=file.filename,
    )


@router.get("/{barcode_id}/edit", name="barcodes_edit")
def edit_barcode_page(request: Request, barcode_id: int):
    database = get_database(request)

    def _operation() -> BarcodeMapping | None:
        with database.session() as session:
            return session.get(BarcodeMapping, barcode_id)

    item = database.run_with_retry(_operation)
    if item is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return get_templates(request).render(request, "barcodes/edit.html", item=item, error=None)


@router.post("/{barcode_id}/edit", name="barcodes_edit_submit")
def edit_barcode(
    request: Request,
    barcode_id: int,
    barcode: str = Form(""),
    surname: str = Form(""),
):
    database = get_database(request)
    templates = get_templates(request)

    def _get() -> BarcodeMapping | None:
        with database.session() as session:
            return session.get(BarcodeMapping, barcode_id)

    item = database.run_with_retry(_get)
    if item is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    try:
        barcode_value = validate_barcode(barcode)
        surname_value = sanitize_surname(surname)
    except ValueError as exc:
        return templates.render(
            request, "barcodes/edit.html", item=item, error=str(exc), status_code=400
        )

    def _update() -> str:
        with database.session() as session:
            current = session.get(BarcodeMapping, barcode_id)
            if current is None:
                return "missing"
            conflict = session.scalar(
                select(BarcodeMapping.id).where(
                    BarcodeMapping.barcode == barcode_value,
                    BarcodeMapping.id != barcode_id,
                )
            )
            if conflict is not None:
                return "duplicate"
            current.barcode = barcode_value
            current.surname = surname_value
            return "updated"

    try:
        result = database.run_with_retry(_update)
    except IntegrityError:
        result = "duplicate"

    if result == "duplicate":
        return templates.render(
            request,
            "barcodes/edit.html",
            item=item,
            error=f"Штрихкод {barcode_value} уже используется другой записью",
            status_code=409,
        )
    if result == "missing":
        raise HTTPException(status_code=404, detail="Запись не найдена")
    set_flash(request, "success", "Запись обновлена")
    return RedirectResponse("/barcodes", status_code=303)


@router.get("/{barcode_id}/delete", name="barcodes_delete_confirm")
def delete_barcode_page(request: Request, barcode_id: int):
    database = get_database(request)

    def _operation() -> BarcodeMapping | None:
        with database.session() as session:
            return session.get(BarcodeMapping, barcode_id)

    item = database.run_with_retry(_operation)
    if item is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return get_templates(request).render(request, "barcodes/delete.html", item=item)


@router.post("/{barcode_id}/delete", name="barcodes_delete")
def delete_barcode(request: Request, barcode_id: int, confirm: str = Form("")):
    if confirm != "yes":
        set_flash(request, "warning", "Удаление отменено")
        return RedirectResponse("/barcodes", status_code=303)
    database = get_database(request)

    def _operation() -> bool:
        with database.session() as session:
            item = session.get(BarcodeMapping, barcode_id)
            if item is None:
                return False
            session.delete(item)
            return True

    if database.run_with_retry(_operation):
        set_flash(request, "success", "Запись удалена")
    else:
        set_flash(request, "warning", "Запись не найдена")
    return RedirectResponse("/barcodes", status_code=303)
