"""Logging configuration: console + rotating file, never inside INPUT_DIR."""

from __future__ import annotations

import contextlib
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONFIGURED = False


def configure_logging(settings: Settings) -> None:
    global _CONFIGURED

    # Restrict permissions of runtime files (logs, SQLite journal files, temp).
    os.umask(0o027)
    root = logging.getLogger()
    level = settings.logging_level()

    # Reconfiguration is used by tests; remove only handlers we installed.
    for handler in list(root.handlers):
        if getattr(handler, "_barcode_sorter_handler", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._barcode_sorter_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if settings.log_dir:
        try:
            settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
            log_path = settings.log_dir / "barcode-sorter.log"
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            with contextlib.suppress(OSError):
                os.chmod(log_path, 0o640)
            file_handler.setFormatter(formatter)
            file_handler._barcode_sorter_handler = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except OSError:
            logging.getLogger(__name__).exception(
                "Could not open log file in %s; continuing with console logging only",
                settings.log_dir,
            )

    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(max(level, logging.INFO))
    logging.getLogger("multipart").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_log_path(settings: Settings) -> Path:
    return settings.log_dir / "barcode-sorter.log"
