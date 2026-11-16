"""Authentication primitives, CSRF and login throttling."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request

from app.config import Settings

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user"
SESSION_CSRF_KEY = "csrf_token"
PBKDF2_ITERATIONS = 600_000


class AuthRequired(Exception):
    """Raised by dependencies; converted to a redirect by the app."""


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password:
        raise ValueError("Пароль не может быть пустым")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def verify_password_hash(password: str, encoded: str) -> bool:
    try:
        scheme, *parts = encoded.split("$")
        if scheme == "pbkdf2_sha256":
            iterations_text, salt_text, digest_text = parts
            iterations = int(iterations_text)
            if iterations < 100_000 or iterations > 10_000_000:
                return False
            salt = _b64decode(salt_text)
            expected = _b64decode(digest_text)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(actual, expected)
        if scheme == "scrypt":
            n_text, r_text, p_text, salt_text, digest_text = parts
            n, r, p = int(n_text), int(r_text), int(p_text)
            if n < 2**14 or n > 2**20 or r < 1 or r > 32 or p < 1 or p > 16:
                return False
            salt = _b64decode(salt_text)
            expected = _b64decode(digest_text)
            actual = hashlib.scrypt(
                password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
            )
            return hmac.compare_digest(actual, expected)
        return False
    except (ValueError, TypeError, IndexError):
        logger.warning("Malformed WEB_PASSWORD_HASH; authentication will fail")
        return False


def verify_password(password: str, settings: Settings) -> bool:
    if not password:
        return False
    if settings.web_password_hash:
        return verify_password_hash(password, settings.web_password_hash)
    if settings.web_password is not None:
        expected = settings.web_password.get_secret_value()
        if not expected:
            return False
        return hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
    return False


def get_session_secret(settings: Settings) -> str:
    """Use SESSION_SECRET or a persistent random secret stored in DATA_DIR."""

    if settings.session_secret is not None and settings.session_secret.get_secret_value():
        return settings.session_secret.get_secret_value()

    secret_path: Path = settings.data_dir / ".session_secret"
    try:
        existing = secret_path.read_text(encoding="ascii").strip()
        if len(existing) >= 32:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Could not read session secret; using an ephemeral secret")

    secret = secrets.token_urlsafe(48)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secret)
                handle.flush()
                os.fsync(handle.fileno())
            return secret
        except Exception:
            with contextlib.suppress(OSError):
                secret_path.unlink(missing_ok=True)
            raise
    except FileExistsError:
        try:
            existing = secret_path.read_text(encoding="ascii").strip()
            if len(existing) >= 32:
                return existing
        except OSError:
            pass
    except OSError:
        logger.exception("Could not persist session secret; using an ephemeral secret")
    return secret


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or len(token) < 20:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency: validate CSRF for state-changing methods.

    Using a dependency (not middleware) lets Starlette cache the parsed form so
    multipart uploads are not consumed twice.
    """

    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    try:
        form = await request.form(
            max_files=2,
            max_fields=100,
            max_part_size=request.app.state.settings.max_upload_bytes,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Некорректная форма запроса") from exc

    supplied = form.get("csrf_token") or request.headers.get("x-csrf-token")
    stored = request.session.get(SESSION_CSRF_KEY)
    if (
        not isinstance(supplied, str)
        or not isinstance(stored, str)
        or not hmac.compare_digest(supplied, stored)
    ):
        raise HTTPException(status_code=403, detail="CSRF-токен недействителен. Обновите страницу.")


def rotate_session_csrf(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[SESSION_CSRF_KEY] = token
    return token


class LoginRateLimiter:
    """Small in-memory brute-force protection (per client address)."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        bucket = self._failures[key]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if not bucket:
            self._failures.pop(key, None)

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            bucket = self._failures.get(key)
            return bool(bucket and len(bucket) >= self.max_failures)

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            self._failures[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"
