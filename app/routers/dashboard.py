"""Dashboard and scanner controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.deps import get_database, get_scanner, get_templates, require_user
from app.security import verify_csrf
from app.services.stats import get_recent_documents, get_stats
from app.templating import set_flash

router = APIRouter(dependencies=[Depends(require_user), Depends(verify_csrf)])


def _dashboard_context(request: Request, message: str | None = None, message_kind: str = "info"):
    database = get_database(request)
    scanner = get_scanner(request)
    stats = get_stats(database)
    recent = get_recent_documents(database, limit=20)
    return {
        "stats": stats.as_dict(),
        "recent": recent,
        "scanner": scanner.status(),
        "scan_message": message,
        "scan_message_kind": message_kind,
    }


def _respond(request: Request, message: str | None = None, kind: str = "info"):
    templates = get_templates(request)
    partial = request.headers.get("HX-Request") == "true"
    context = _dashboard_context(request, message, kind)
    if partial:
        return templates.render(
            request,
            "partials/dashboard_content.html",
            consume_flashes=False,
            **context,
        )
    if message:
        set_flash(request, kind, message)
    return RedirectResponse("/", status_code=303)


@router.get("/", name="dashboard")
def dashboard(request: Request):
    templates = get_templates(request)
    return templates.render(request, "dashboard.html", **_dashboard_context(request))


@router.get("/partials/dashboard", name="dashboard_partial")
def dashboard_partial(request: Request):
    templates = get_templates(request)
    return templates.render(
        request,
        "partials/dashboard_content.html",
        consume_flashes=False,
        **_dashboard_context(request),
    )


@router.post("/scan", name="scan_once")
def request_scan(request: Request):
    scanner = get_scanner(request)
    if scanner.request_scan():
        message, kind = "Полное сканирование запущено", "info"
    else:
        message, kind = "Сканер не запущен (SCANNER_ENABLED=false)", "warning"
    return _respond(request, message, kind)


@router.post("/scan/pause", name="scan_pause")
def pause_scan(request: Request):
    get_scanner(request).pause()
    return _respond(request, "Автоматическое сканирование приостановлено", "warning")


@router.post("/scan/resume", name="scan_resume")
def resume_scan(request: Request):
    get_scanner(request).resume()
    return _respond(request, "Автоматическое сканирование возобновлено", "success")
