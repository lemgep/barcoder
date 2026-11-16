"""FastAPI dependencies shared by routers."""

from __future__ import annotations

from fastapi import Depends, Request

from app.config import Settings
from app.database import Database
from app.security import SESSION_USER_KEY, AuthRequired, ensure_csrf_token
from app.services.scanner import ScannerService
from app.templating import AppTemplates


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_scanner(request: Request) -> ScannerService:
    return request.app.state.scanner


def get_templates(request: Request) -> AppTemplates:
    return request.app.state.templates


def get_current_user(request: Request) -> str | None:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return "anonymous"
    user = request.session.get(SESSION_USER_KEY)
    return user if isinstance(user, str) and user else None


def require_user(request: Request) -> str:
    user = get_current_user(request)
    if user is None:
        raise AuthRequired()
    return user


def csrf_context(request: Request) -> str:
    return ensure_csrf_token(request)


# Re-export for router wiring convenience.
__all__ = [
    "Depends",
    "csrf_context",
    "get_current_user",
    "get_database",
    "get_scanner",
    "get_settings",
    "get_templates",
    "require_user",
]
