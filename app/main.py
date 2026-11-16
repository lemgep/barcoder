"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.config import Settings, load_settings
from app.database import Database
from app.logging_config import configure_logging
from app.models import utc_now
from app.routers import auth, barcodes, dashboard, logs
from app.security import AuthRequired, LoginRateLimiter, get_session_secret
from app.services.barcode_reader import BarcodeReader
from app.services.document_processor import DocumentProcessor
from app.services.file_copier import FileCopier
from app.services.safety import inspect_input_readonly
from app.services.scanner import ScannerService
from app.templating import AppTemplates

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class SecurityHeadersMiddleware:
    """Small pure-ASGI middleware adding defensive response headers."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (
                            b"content-security-policy",
                            (
                                b"default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                b"script-src 'self'; connect-src 'self'; base-uri 'none'; "
                                b"form-action 'self'; frame-ancestors 'none'; object-src 'none'"
                            ),
                        ),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate_layout()
    settings.ensure_runtime_dirs()
    configure_logging(settings)

    database = Database(settings)
    reader = BarcodeReader(settings)  # validates BARCODE_FORMATS at startup
    copier = FileCopier(settings)
    processor = DocumentProcessor(settings, database, reader, copier)
    scanner = ScannerService(settings, database, processor)
    templates = AppTemplates(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.init_schema()
        database.recover_interrupted()
        readonly, readonly_message = inspect_input_readonly(settings)
        logger.info(
            "Starting Barcode Document Sorter %s (input=%s, output=%s, db=%s)",
            __version__,
            settings.input_dir,
            settings.output_dir,
            settings.database_path,
        )
        if readonly:
            logger.info("%s", readonly_message)
        else:
            logger.warning("%s", readonly_message)
        if not settings.auth_enabled:
            logger.warning(
                "Web authentication is DISABLED (AUTH_ENABLED=false). "
                "Never expose this interface to untrusted networks."
            )
        if settings.scanner_enabled:
            await scanner.start()
        else:
            logger.warning("Automatic scanner is disabled (SCANNER_ENABLED=false)")
        app.state.started_at = utc_now()
        try:
            yield
        finally:
            logger.info("Shutting down")
            await scanner.stop()
            database.dispose()

    app = FastAPI(
        title="Barcode Document Sorter",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db = database
    app.state.reader = reader
    app.state.copier = copier
    app.state.processor = processor
    app.state.scanner = scanner
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(barcodes.router)
    app.include_router(logs.router)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request):
        db_ok = await asyncio.to_thread(database.healthcheck)
        scanner_state = scanner.status()
        payload = {
            "status": "ok" if db_ok else "error",
            "version": __version__,
            "database": db_ok,
            "input_available": scanner_state["input_available"],
            "input_readonly": scanner_state["input_readonly"],
            "scanner_running": scanner_state["running"],
            "scanner_paused": scanner_state["paused"],
        }
        return JSONResponse(payload, status_code=200 if db_ok else 503)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.exception_handler(AuthRequired)
    async def auth_required_handler(request: Request, exc: AuthRequired):
        if request.headers.get("HX-Request") == "true":
            return PlainTextResponse(
                "Требуется авторизация", status_code=401, headers={"HX-Redirect": "/login"}
            )
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/healthz"):
            return JSONResponse({"status": "error", "detail": exc.detail}, status_code=exc.status_code)
        if request.headers.get("HX-Request") == "true":
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        default_messages = {
            400: "Некорректный запрос",
            401: "Требуется авторизация",
            403: "Доступ запрещён",
            404: "Страница не найдена",
            405: "Метод не поддерживается",
            413: "Запрос слишком большой",
            422: "Некорректные данные",
            500: "Внутренняя ошибка сервера",
        }
        message = default_messages.get(exc.status_code, str(exc.detail))
        return templates.render(
            request,
            "error.html",
            status_code=exc.status_code,
            message=message,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.warning("Request validation failed for %s", request.url.path)
        if request.headers.get("HX-Request") == "true":
            return PlainTextResponse("Некорректные данные запроса", status_code=400)
        return templates.render(
            request,
            "error.html",
            status_code=400,
            message="Некорректные данные формы. Проверьте заполнение полей.",
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled web error on %s", request.url.path)
        if request.url.path.startswith("/healthz"):
            return JSONResponse({"status": "error"}, status_code=500)
        if request.headers.get("HX-Request") == "true":
            return PlainTextResponse("Внутренняя ошибка сервера", status_code=500)
        return templates.render(
            request,
            "error.html",
            status_code=500,
            message="Внутренняя ошибка сервера. Подробности записаны в журнал.",
        )

    # Middleware order: last added is outermost.
    app.add_middleware(SecurityHeadersMiddleware)
    allowed_hosts = [host.strip() for host in settings.allowed_hosts.split(",") if host.strip()]
    if allowed_hosts and allowed_hosts != ["*"]:
        # Internal healthcheck connects to 127.0.0.1 and must stay healthy even
        # when the public hostname is restricted.
        for loopback in ("127.0.0.1", "localhost"):
            if loopback not in allowed_hosts:
                allowed_hosts.append(loopback)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_session_secret(settings),
        session_cookie="barcode_sorter_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    return app


def main() -> None:  # pragma: no cover - local development helper
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
