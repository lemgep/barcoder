"""Safe output writing: path sanitising, atomic no-overwrite copies."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.constants import MAX_COMPONENT_BYTES, SERVICE_DIRECTORIES
from app.services.errors import ProcessingError

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_COPY_CHUNK = 1024 * 1024


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    encoded = value.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


def sanitize_component(value: str, *, fallback: str | None = None, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Turn an arbitrary surname or directory label into one safe path segment.

    The function removes separators, control characters, leading dots and
    trailing spaces/dots.  It never returns ``.`` or ``..``.
    """

    text = unicodedata.normalize("NFC", str(value or ""))
    text = _UNSAFE_CHARS.sub("_", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.strip(" .")
    if not text or text in {".", ".."}:
        if fallback is None:
            raise ValueError("Пустое или недопустимое имя")
        text = fallback
    if text.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        text = f"_{text}"
    text = _truncate_utf8(text, max_bytes).strip(" .")
    if not text:
        if fallback is None:
            raise ValueError("Пустое или недопустимое имя")
        text = fallback
    return text


def sanitize_surname(value: str) -> str:
    """Validate a surname intended for the database and a directory name."""

    normalized = unicodedata.normalize("NFC", str(value or "")).strip()
    if not normalized:
        raise ValueError("Фамилия не может быть пустой")
    safe = sanitize_component(normalized, max_bytes=150)
    if safe != normalized:
        raise ValueError(
            "Фамилия содержит недопустимые для имени директории символы "
            "(разделители пути, управляющие символы, точки по краям и т.п.)"
        )
    if len(safe.encode("utf-8")) > 150:
        raise ValueError("Фамилия слишком длинная")
    if safe.upper() in SERVICE_DIRECTORIES:
        raise ValueError("Фамилия совпадает с названием служебной директории")
    return safe


def sanitize_filename(value: str) -> str:
    """Return a safe single-component filename, preserving its extension."""

    raw = str(value or "").replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFC", raw)
    stem, suffix = os.path.splitext(raw)
    safe_stem = sanitize_component(stem, fallback="document", max_bytes=180)
    suffix = _UNSAFE_CHARS.sub("", suffix)
    if suffix and len(suffix) > 20:
        suffix = suffix[:20]
    result = f"{safe_stem}{suffix}"
    if not result or result in {".", ".."}:
        return "document"
    return result


@dataclass(frozen=True)
class CopyResult:
    path: Path
    size: int
    reused: bool = False


class TargetExistsError(ProcessingError):
    """The explicitly requested target already exists; choose a new name."""

    def __init__(self, target: Path) -> None:
        super().__init__(f"Файл уже существует: {target}", retryable=False)
        self.target = target


class FileCopier:
    """Writes only inside OUTPUT_DIR, with hard guarantees against overwrites."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.input_root = settings.input_dir.expanduser().resolve(strict=False)
        self.output_root = settings.output_dir.expanduser().resolve(strict=False)
        self._prepared = False
        self._prepare_lock = threading.Lock()
        if self.output_root.is_relative_to(self.input_root) or self.input_root.is_relative_to(
            self.output_root
        ):
            raise RuntimeError(
                "OUTPUT_DIR и INPUT_DIR не должны пересекаться; запись в INPUT_DIR запрещена"
            )

    def _ensure_root(self) -> None:
        """Create writable directories lazily so startup survives outages."""

        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            if self.output_root.is_symlink():
                raise ProcessingError(
                    "OUTPUT_DIR не может быть символической ссылкой", retryable=False
                )
            try:
                self.output_root.mkdir(parents=True, exist_ok=True, mode=0o750)
            except OSError as exc:
                raise ProcessingError(
                    f"Выходная директория недоступна: {exc}", retryable=True
                ) from exc
            if self.output_root.is_symlink() or not self.output_root.is_dir():
                raise ProcessingError(
                    "OUTPUT_DIR не является корректной директорией", retryable=False
                )
            for name in SERVICE_DIRECTORIES:
                self._ensure_directory_component(name)
            self._prepared = True

    def _assert_output_path(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        if resolved != self.output_root and not resolved.is_relative_to(self.output_root):
            raise ProcessingError(
                f"Попытка записи за пределы OUTPUT_DIR заблокирована: {path}", retryable=False
            )
        if resolved == self.input_root or resolved.is_relative_to(self.input_root):
            raise ProcessingError(
                f"Попытка записи в INPUT_DIR заблокирована: {path}", retryable=False
            )

    def ensure_directory(self, name: str) -> Path:
        self._ensure_root()
        return self._ensure_directory_component(name)

    def _ensure_directory_component(self, name: str) -> Path:
        safe_name = sanitize_component(name, max_bytes=150)
        directory = self.output_root / safe_name
        if directory.is_symlink():
            raise ProcessingError(
                f"Директория {directory} является символической ссылкой; запись запрещена",
                retryable=False,
            )
        try:
            directory.mkdir(mode=0o750, exist_ok=True)
        except OSError as exc:
            raise ProcessingError(
                f"Не удалось создать выходную директорию {directory}: {exc}", retryable=True
            ) from exc
        if directory.is_symlink() or not directory.is_dir():
            raise ProcessingError(f"Некорректная выходная директория: {directory}", retryable=False)
        self._assert_output_path(directory)
        return directory

    def assert_source(self, source: Path) -> Path:
        """Public read-only validation used by the processor as well."""

        self._assert_source(source)
        return source.resolve(strict=True)

    def validate_target(self, target: Path) -> None:
        self._assert_output_path(Path(target))

    def choose_target(self, directory_name: str, filename: str) -> Path:
        """Return a non-existing target path without creating anything."""

        destination_dir = self.ensure_directory(directory_name)
        safe_filename = sanitize_filename(filename)
        for candidate in self._candidate_names(safe_filename):
            target = destination_dir / candidate
            self._assert_output_path(target)
            if target.is_symlink() or target.exists():
                continue
            return target
        raise ProcessingError(
            "Не удалось подобрать уникальное имя файла в выходной директории",
            retryable=False,
        )

    def copy_to_target(
        self,
        source: Path,
        target: Path,
        *,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
    ) -> CopyResult:
        """Copy to an exact, pre-reserved path without ever overwriting."""

        source = Path(source)
        target = Path(target)
        self._assert_source(source)
        self._assert_output_path(target)
        if target.is_symlink():
            raise TargetExistsError(target)
        if target.parent != self.output_root and not target.parent.exists():
            # A previously reserved surname directory may have been removed
            # externally. Recreate only a direct child of OUTPUT_DIR.
            if target.parent.parent.resolve(strict=False) != self.output_root:
                raise ProcessingError(
                    "Родительская выходная директория недоступна", retryable=True
                )
            self.ensure_directory(target.parent.name)
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ProcessingError(f"Не удалось прочитать исходный файл: {exc}") from exc
        self._check_free_space(target.parent, size)
        return self._copy_atomically(
            source,
            target.parent,
            sanitize_filename(target.name),
            explicit_target=target,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
        )

    def copy_to_directory(
        self,
        source: Path,
        directory_name: str,
        *,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
    ) -> CopyResult:
        source = Path(source)
        self._assert_source(source)
        for _ in range(100):
            target = self.choose_target(directory_name, source.name)
            try:
                return self.copy_to_target(
                    source,
                    target,
                    expected_size=expected_size,
                    expected_mtime_ns=expected_mtime_ns,
                )
            except TargetExistsError:
                continue
        raise ProcessingError(
            "Не удалось подобрать уникальное имя файла в выходной директории",
            retryable=False,
        )

    # ------------------------------------------------------------- internals
    def _assert_source(self, source: Path) -> None:
        if source.is_symlink():
            raise ProcessingError("Символические ссылки в INPUT_DIR не обрабатываются", retryable=False)
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ProcessingError(f"Исходный файл недоступен: {exc}") from exc
        if resolved == self.input_root or not resolved.is_relative_to(self.input_root):
            raise ProcessingError(
                "Исходный файл находится вне INPUT_DIR; обработка запрещена", retryable=False
            )
        if resolved.is_relative_to(self.output_root):
            raise ProcessingError(
                "Исходный файл находится в OUTPUT_DIR; обработка запрещена", retryable=False
            )

    def _check_free_space(self, directory: Path, size: int) -> None:
        try:
            usage = shutil.disk_usage(directory)
        except OSError:
            return  # Some network filesystems do not report free space.
        if usage.free < size + 8 * 1024 * 1024:
            raise ProcessingError(
                f"Недостаточно свободного места для копирования {size} байт", retryable=True
            )

    def _candidate_names(self, filename: str) -> list[str]:
        stem, suffix = os.path.splitext(filename)
        yield filename
        for index in range(1, 10000):
            yield f"{stem}_{index}{suffix}"
        yield f"{stem}_{uuid.uuid4().hex[:12]}{suffix}"

    def _copy_atomically(
        self,
        source: Path,
        directory: Path,
        filename: str,
        *,
        explicit_target: Path | None = None,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
    ) -> CopyResult:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(source, flags)
        except OSError as exc:
            raise ProcessingError(f"Ошибка чтения исходного файла: {exc}") from exc

        temp_path: Path | None = None
        try:
            with os.fdopen(source_fd, "rb", closefd=True) as source_file:
                before = os.fstat(source_file.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ProcessingError("Исходный объект не является обычным файлом", retryable=False)
                if expected_size is not None and before.st_size != expected_size:
                    raise ProcessingError(
                        "Исходный файл изменился после вычисления SHA-256; попытка будет повторена",
                        retryable=True,
                    )
                if expected_mtime_ns is not None and before.st_mtime_ns != expected_mtime_ns:
                    raise ProcessingError(
                        "Исходный файл изменился после вычисления SHA-256; попытка будет повторена",
                        retryable=True,
                    )

                fd, temp_name = tempfile.mkstemp(
                    prefix=".barcode-sorter-", suffix=".part", dir=directory
                )
                temp_path = Path(temp_name)
                with os.fdopen(fd, "wb") as destination:
                    shutil.copyfileobj(source_file, destination, length=_COPY_CHUNK)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.chmod(temp_path, 0o640)

                after = os.fstat(source_file.fileno())
                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or temp_path.stat().st_size != before.st_size
                ):
                    raise ProcessingError(
                        "Исходный файл изменился во время копирования; попытка будет повторена",
                        retryable=True,
                    )

            if explicit_target is not None:
                candidates = (explicit_target,)
            else:
                candidates = tuple(
                    directory / candidate for candidate in self._candidate_names(filename)
                )

            for target in candidates:
                self._assert_output_path(target)
                if target.is_symlink() or target.exists():
                    if explicit_target is not None:
                        raise TargetExistsError(target)
                    continue
                try:
                    os.link(temp_path, target)
                except FileExistsError as exc:
                    if explicit_target is not None:
                        raise TargetExistsError(target) from exc
                    continue
                except OSError as exc:
                    if exc.errno not in {
                        errno.EPERM,
                        errno.EACCES,
                        errno.EOPNOTSUPP,
                        errno.ENOTSUP,
                        errno.EXDEV,
                        errno.EMLINK,
                    }:
                        raise
                    # Filesystem without hard links: exclusive create + copy.
                    if self._copy_without_overwrite(temp_path, target):
                        pass
                    elif target.exists():
                        if explicit_target is not None:
                            raise TargetExistsError(target) from exc
                        continue
                    else:
                        raise
                try:
                    os.unlink(temp_path)
                except OSError:
                    logger.warning("Could not remove temporary copy %s", temp_path)
                else:
                    temp_path = None
                return CopyResult(path=target, size=before.st_size)

            raise ProcessingError(
                "Не удалось подобрать уникальное имя файла в выходной директории", retryable=False
            )
        except ProcessingError:
            raise
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise ProcessingError("Недостаточно свободного места на диске", retryable=True) from exc
            raise ProcessingError(f"Ошибка копирования файла: {exc}", retryable=True) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not clean up temporary file %s", temp_path)

    @staticmethod
    def _copy_without_overwrite(temp_path: Path, target: Path) -> bool:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            target_fd = os.open(target, flags, 0o640)
        except FileExistsError:
            return False
        try:
            with os.fdopen(target_fd, "wb") as destination, temp_path.open("rb") as source:
                shutil.copyfileobj(source, destination, length=_COPY_CHUNK)
                destination.flush()
                os.fsync(destination.fileno())
            return True
        except Exception:
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
            raise
