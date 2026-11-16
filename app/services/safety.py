"""Input-directory safety checks (read-only verification without writing)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)


def inspect_input_readonly(settings: Settings) -> tuple[bool, str]:
    """Return (read_only, human-readable description).

    The check is purely observational: it never creates a probe file in the
    source directory.  Docker's ``:ro`` mount is the primary guarantee; this
    check catches misconfigured local runs as well.
    """

    path: Path = settings.input_dir
    try:
        if not path.exists():
            return False, "Исходная директория недоступна"
        if not path.is_dir():
            return False, "INPUT_DIR не является директорией"
    except OSError as exc:
        return False, f"Исходная директория недоступна: {exc}"

    readonly_flag = False
    try:
        statvfs = os.statvfs(path)
        flag = getattr(os, "ST_RDONLY", 1)
        readonly_flag = bool(statvfs.f_flag & flag)
    except (OSError, AttributeError):
        pass

    writable = os.access(path, os.W_OK)
    if readonly_flag or not writable:
        return True, "Исходная директория защищена от записи"

    return (
        False,
        (
            "ВНИМАНИЕ: INPUT_DIR доступна для записи. Приложение никогда не пишет в неё, "
            "но рекомендуется монтировать папку с флагом :ro"
        ),
    )
