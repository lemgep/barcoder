"""Jinja2 rendering helpers and template filters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from starlette.templating import Jinja2Templates

from app import __version__
from app.config import Settings
from app.models import STATUS_CSS, STATUS_LABELS
from app.security import SESSION_USER_KEY, ensure_csrf_token

TEMPLATE_DIR = Path(__file__).parent / "templates"


class AppTemplates:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tz = ZoneInfo(settings.tz)
        self.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
        env = self.templates.env
        env.globals.update(
            status_label=self.status_label,
            status_css=self.status_css,
            format_datetime=self.format_datetime,
            format_duration=self.format_duration,
            app_version=__version__,
            app_tz=settings.tz,
        )

    @staticmethod
    def status_label(status: str | None) -> str:
        return STATUS_LABELS.get(status or "", status or "—")

    @staticmethod
    def status_css(status: str | None) -> str:
        return STATUS_CSS.get(status or "", "secondary")

    def format_datetime(self, value: datetime | None, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
        if value is None:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self.tz).strftime(fmt)

    @staticmethod
    def format_duration(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        if seconds < 1:
            return f"{seconds * 1000:.0f} мс"
        if seconds < 60:
            return f"{seconds:.1f} с"
        minutes, rest = divmod(int(seconds), 60)
        return f"{minutes} мин {rest} с"

    def render(
        self,
        request: Request,
        name: str,
        *,
        status_code: int = 200,
        consume_flashes: bool = True,
        **context: Any,
    ):
        settings: Settings = request.app.state.settings
        user = None
        if settings.auth_enabled:
            candidate = request.session.get(SESSION_USER_KEY)
            user = candidate if isinstance(candidate, str) else None
        else:
            user = "anonymous"
        flashes = request.session.pop("flashes", []) if consume_flashes else []
        context.update(
            {
                "request": request,
                "csrf_token": ensure_csrf_token(request),
                "current_user": user,
                "flashes": flashes,
                "settings": settings,
                "http_status": status_code,
            }
        )
        return self.templates.TemplateResponse(
            request=request,
            name=name,
            context=context,
            status_code=status_code,
        )


def set_flash(request: Request, category: str, message: str) -> None:
    flashes = request.session.setdefault("flashes", [])
    if not isinstance(flashes, list):
        flashes = []
        request.session["flashes"] = flashes
    flashes.append({"category": category, "message": message})
