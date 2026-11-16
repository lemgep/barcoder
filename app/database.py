"""SQLite access layer with WAL, busy timeout and retry on lock errors."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TypeVar

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    STATUS_PENDING,
    STATUS_PROCESSING,
    Base,
    Document,
    ProcessingLog,
    utc_now,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_LOCK_MARKERS = ("database is locked", "database table is locked", "database schema is locked")


class Database:
    """Owns the SQLAlchemy engine for one application instance.

    A separate instance per app (instead of module-level globals) keeps tests
    isolated and makes the lifecycle explicit.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"timeout": 30, "check_same_thread": False},
            future=True,
        )
        self._install_pragmas(self.engine)
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
            class_=Session,
        )

    @staticmethod
    def _install_pragmas(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA temp_store=MEMORY")
            finally:
                cursor.close()

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def run_with_retry(self, operation: Callable[[], T], attempts: int = 6) -> T:
        """Run a DB operation, retrying transient SQLite lock errors."""

        delay = 0.1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except OperationalError as exc:
                message = str(exc).lower()
                if not any(marker in message for marker in _LOCK_MARKERS):
                    raise
                last_error = exc
                if attempt == attempts:
                    break
                logger.warning("SQLite is locked (attempt %s/%s), retrying", attempt, attempts)
                time.sleep(delay)
                delay = min(delay * 2, 2.0)
        if last_error is None:
            raise RuntimeError("DB retry loop finished without an error or result")
        raise last_error

    def recover_interrupted(self) -> int:
        """Return documents left in 'processing' after a crash to pending."""

        def _operation() -> int:
            recovered = 0
            with self.session() as session:
                docs = session.scalars(
                    select(Document).where(Document.status == STATUS_PROCESSING)
                ).all()
                for doc in docs:
                    doc.status = STATUS_PENDING
                    doc.error_message = "Обработка была прервана; документ возвращён в очередь"
                    doc.updated_at = utc_now()
                    session.add(
                        ProcessingLog(
                            document_id=doc.id,
                            level="WARNING",
                            file_name=doc.file_name,
                            source_path=doc.source_path,
                            status=STATUS_PENDING,
                            message="Обработка была прервана (перезапуск), документ возвращён в очередь",
                        )
                    )
                    recovered += 1
            if recovered:
                logger.warning("Recovered %s interrupted document(s) from 'processing'", recovered)
            return recovered

        return self.run_with_retry(_operation)

    def checkpoint(self) -> None:
        try:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.commit()
        except (OperationalError, sqlite3.Error):
            logger.warning("Could not checkpoint SQLite WAL", exc_info=True)

    def healthcheck(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("Database healthcheck failed")
            return False

    def dispose(self) -> None:
        self.checkpoint()
        self.engine.dispose()
