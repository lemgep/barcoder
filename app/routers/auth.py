"""Authentication routes."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.deps import get_current_user, get_settings, get_templates, require_user
from app.security import (
    SESSION_USER_KEY,
    client_address,
    rotate_session_csrf,
    verify_csrf,
    verify_password,
)
from app.templating import set_flash

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login", name="login")
def login_page(request: Request):
    if get_current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    templates = get_templates(request)
    return templates.render(request, "login.html", error=None)


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    settings = get_settings(request)
    templates = get_templates(request)
    limiter = request.app.state.login_limiter
    address = client_address(request)

    if limiter.is_blocked(address):
        logger.warning("Login blocked by rate limiter for %s", address)
        return templates.render(
            request,
            "login.html",
            error="Слишком много неудачных попыток. Повторите через несколько минут.",
            status_code=429,
        )

    user_matches = hmac.compare_digest(username.strip(), settings.web_username)
    password_matches = verify_password(password, settings)
    if user_matches and password_matches:
        limiter.reset(address)
        request.session.clear()
        request.session[SESSION_USER_KEY] = settings.web_username
        rotate_session_csrf(request)
        logger.info("Successful login for user %r from %s", settings.web_username, address)
        set_flash(request, "success", "Вы вошли в систему")
        return RedirectResponse("/", status_code=303)

    limiter.record_failure(address)
    logger.warning("Failed login attempt from %s", address)
    return templates.render(
        request,
        "login.html",
        error="Неверное имя пользователя или пароль",
        status_code=400,
    )


@router.post("/logout", dependencies=[Depends(require_user), Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    set_flash(request, "info", "Вы вышли из системы")
    return RedirectResponse("/login", status_code=303)
