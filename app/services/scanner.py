"""Periodic, non-blocking directory scanner.

Design notes
------------
* Polling (not inotify) because SMB/NFS file events are unreliable.
* Only one scan pass can run at a time (asyncio.Lock); manual trigger cannot
  start a parallel pass.
* Stability is verified with N checks separated by FILE_STABILITY_INTERVAL
  before a document enters the processing queue.
* Every document is processed in a worker thread, with a semaphore limiting
  concurrent documents.  The event loop (and therefore the web UI) stays free.
* All state needed for restart recovery lives in SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.models import (
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_PROCESSING,
    TERMINAL_STATUSES,
    Document,
    utc_now,
)
from app.services.barcode_reader import SUPPORTED_SUFFIXES
from app.services.document_processor import DocumentProcessor, ProcessOutcome
from app.services.safety import inspect_input_readonly

logger = logging.getLogger(__name__)

_IGNORED_SUFFIXES = {
    ".part",
    ".partial",
    ".tmp",
    ".temp",
    ".crdownload",
    ".download",
    ".lock",
    ".swp",
}
_IGNORED_PREFIXES = (".", "~$", "._")


@dataclass(frozen=True)
class LatestDocumentState:
    status: str
    size_bytes: int
    source_mtime_ns: int
    attempts: int
    next_retry_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ScanCandidate:
    absolute_path: Path
    relative_path: str
    size: int
    mtime_ns: int
    is_new: bool


class ScannerUnavailable(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class ScannerService:
    """Long-running polling scanner owned by the FastAPI application."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        processor: DocumentProcessor,
    ) -> None:
        self.settings = settings
        self.db = database
        self.processor = processor
        self.input_root = settings.input_dir.expanduser().resolve(strict=False)

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._scan_lock = asyncio.Lock()

        self._paused = False
        self._running = False
        self._scanning = False
        self._manual_requested = False
        self._active: set[str] = set()

        self.last_scan_at: datetime | None = None
        self.last_scan_started_at: datetime | None = None
        self.last_scan_duration: float | None = None
        self.last_new_files = 0
        self.last_processed = 0
        self.last_errors = 0
        self.last_status_counts: dict[str, int] = {}
        self.last_error: str | None = None
        self.input_available: bool | None = None
        self.input_readonly: bool | None = None
        self.input_readonly_message: str = ""
        self._unavailable_logged_at: float = 0.0
        self._readonly_warned_at: float = 0.0
        self._consecutive_failures = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._running = True
        self._task = asyncio.create_task(self._run(), name="barcode-scanner")
        logger.info(
            "Scanner started (interval=%ss, stability=%s checks every %ss, workers=%s)",
            self.settings.scan_interval,
            self.settings.file_stability_checks,
            self.settings.file_stability_interval,
            self.settings.max_file_workers,
        )

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is None:
            return
        # Give the current document a chance to finish before cancelling.
        grace = max(10.0, min(self.settings.max_document_seconds + 5.0, 35.0))
        try:
            await asyncio.wait_for(task, timeout=grace)
        except TimeoutError:
            logger.warning("Scanner did not stop in time; cancelling")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._scanning = False
            logger.info("Scanner stopped")

    # -------------------------------------------------------------- controls
    @property
    def running(self) -> bool:
        return bool(self._running and self._task and not self._task.done())

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        self._wake.set()
        logger.info("Automatic scanning paused")

    def resume(self) -> None:
        self._paused = False
        self._wake.set()
        logger.info("Automatic scanning resumed")

    def request_scan(self) -> bool:
        """Request an immediate pass; never starts a second concurrent pass."""

        if not self.running:
            return False
        self._manual_requested = True
        self._wake.set()
        return True

    def status(self) -> dict[str, object]:
        return {
            "running": self.running,
            "paused": self._paused,
            "scanning": self._scanning,
            "manual_pending": self._manual_requested,
            "active_documents": len(self._active),
            "last_scan_at": self.last_scan_at,
            "last_scan_started_at": self.last_scan_started_at,
            "last_scan_duration": self.last_scan_duration,
            "last_new_files": self.last_new_files,
            "last_processed": self.last_processed,
            "last_errors": self.last_errors,
            "last_status_counts": dict(self.last_status_counts),
            "last_error": self.last_error,
            "input_available": self.input_available,
            "input_readonly": self.input_readonly,
            "input_readonly_message": self.input_readonly_message,
            "scan_interval": self.settings.scan_interval,
            "consecutive_failures": self._consecutive_failures,
        }

    # ------------------------------------------------------------------ loop
    async def _run(self) -> None:
        logger.info("Scanner loop started")
        try:
            while not self._stop.is_set():
                force = self._manual_requested
                self._manual_requested = False

                if self._paused and not force:
                    await self._wait_for_wake(timeout=1.0)
                    continue

                try:
                    await self.scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Unhandled scanner error")
                    self.last_error = f"Непредвиденная ошибка сканера: {exc}"
                    self._consecutive_failures += 1

                # Normal interval after every pass, including a manual one.
                # A new manual request or pause/resume wakes us immediately.
                await self._wait_for_wake(timeout=self.settings.scan_interval)
        except asyncio.CancelledError:
            logger.info("Scanner loop cancelled")
            raise
        finally:
            logger.info("Scanner loop finished")

    async def _wait_for_wake(self, timeout: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)

    # ------------------------------------------------------------- scan pass
    async def scan_once(self) -> dict[str, int]:
        """Run one complete pass.  Returns counters (also used by tests)."""

        if self._scan_lock.locked():
            logger.info("Scan request ignored: a scan pass is already running")
            return {"new": 0, "processed": 0, "errors": 0}

        async with self._scan_lock:
            self._scanning = True
            started = time.monotonic()
            self.last_scan_started_at = utc_now()
            counts = {"new": 0, "processed": 0, "errors": 0}
            self.last_status_counts = {}
            try:
                readonly, readonly_message = inspect_input_readonly(self.settings)
                self.input_readonly = readonly
                self.input_readonly_message = readonly_message
                if not readonly:
                    now_monotonic = time.monotonic()
                    if now_monotonic - self._readonly_warned_at > 300:
                        logger.warning("%s", readonly_message)
                        self._readonly_warned_at = now_monotonic

                try:
                    candidates, skipped = await asyncio.to_thread(self._collect_candidates)
                except ScannerUnavailable as exc:
                    self._handle_unavailable(exc.message)
                    return counts
                except Exception as exc:
                    logger.exception("Could not enumerate INPUT_DIR")
                    self._handle_unavailable(f"Ошибка чтения INPUT_DIR: {exc}")
                    return counts

                self.input_available = True
                self.last_error = None
                self._consecutive_failures = 0
                self.last_new_files = sum(1 for candidate in candidates if candidate.is_new)
                counts["new"] = self.last_new_files

                if skipped:
                    logger.debug("Scanner skipped %s already-processed file(s)", skipped)
                if not candidates:
                    self.last_processed = 0
                    self.last_errors = 0
                    return counts

                # Stability checks run concurrently; each candidate sleeps
                # FILE_STABILITY_INTERVAL between stat calls.
                stable_checks = await asyncio.gather(
                    *(self._wait_until_stable(candidate) for candidate in candidates)
                )
                stable = [
                    candidate
                    for candidate, is_stable in zip(candidates, stable_checks, strict=True)
                    if is_stable
                ]

                if not stable:
                    self.last_processed = 0
                    self.last_errors = 0
                    return counts

                semaphore = asyncio.Semaphore(self.settings.max_file_workers)

                async def run_one(candidate: ScanCandidate) -> ProcessOutcome | None:
                    async with semaphore:
                        if candidate.relative_path in self._active:
                            return None
                        self._active.add(candidate.relative_path)
                        try:
                            return await asyncio.to_thread(
                                self.processor.process, candidate.absolute_path
                            )
                        except Exception as exc:
                            logger.exception("Worker failed for %s", candidate.relative_path)
                            return ProcessOutcome(
                                status=STATUS_ERROR,
                                error=f"Ошибка обработки: {exc}",
                            )
                        finally:
                            self._active.discard(candidate.relative_path)

                outcomes = await asyncio.gather(*(run_one(candidate) for candidate in stable))
                for outcome in outcomes:
                    if outcome is None or outcome.skipped:
                        continue
                    counts["processed"] += 1
                    self.last_status_counts[outcome.status] = (
                        self.last_status_counts.get(outcome.status, 0) + 1
                    )
                    if outcome.status == STATUS_ERROR:
                        counts["errors"] += 1

                self.last_processed = counts["processed"]
                self.last_errors = counts["errors"]
                if counts["new"] or counts["processed"]:
                    logger.info(
                        "Scan pass finished: new=%s processed=%s errors=%s",
                        counts["new"],
                        counts["processed"],
                        counts["errors"],
                    )
                return counts
            finally:
                self.last_scan_duration = time.monotonic() - started
                self.last_scan_at = utc_now()
                self._scanning = False

    def _handle_unavailable(self, message: str) -> None:
        self.input_available = False
        self.last_error = message
        self._consecutive_failures += 1
        self.last_new_files = 0
        self.last_processed = 0
        self.last_errors = 0
        now = time.monotonic()
        if now - self._unavailable_logged_at > 60:
            logger.warning("%s; scanner will retry", message)
            self._unavailable_logged_at = now

    async def _wait_until_stable(self, candidate: ScanCandidate) -> bool:
        previous: tuple[int, int, int, int] | None = None
        for check_index in range(self.settings.file_stability_checks):
            if self._stop.is_set():
                return False
            try:
                info = await asyncio.to_thread(os.stat, candidate.absolute_path, follow_symlinks=False)
            except OSError:
                return False
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return False
            signature = (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)
            if previous is not None and signature != previous:
                logger.debug("File %s is still changing; will retry next pass", candidate.relative_path)
                return False
            previous = signature
            if check_index < self.settings.file_stability_checks - 1:
                await asyncio.sleep(self.settings.file_stability_interval)
        return True

    # --------------------------------------------------------- directory walk
    def _collect_candidates(self) -> tuple[list[ScanCandidate], int]:
        """Runs in a worker thread: walk + DB dedup filtering."""

        if not self.input_root.exists():
            raise ScannerUnavailable(f"Исходная директория недоступна: {self.input_root}")
        if not self.input_root.is_dir():
            raise ScannerUnavailable(f"INPUT_DIR не является директорией: {self.input_root}")

        found: list[tuple[str, Path, int, int]] = []
        walk_errors: list[str] = []

        def on_error(error: OSError) -> None:
            walk_errors.append(str(error))

        try:
            for root, dirs, files in os.walk(
                self.input_root, topdown=True, followlinks=False, onerror=on_error
            ):
                dirs[:] = sorted(
                    directory
                    for directory in dirs
                    if not directory.startswith(".") and not Path(root, directory).is_symlink()
                )
                for name in files:
                    if name.startswith(_IGNORED_PREFIXES):
                        continue
                    suffix = Path(name).suffix.lower()
                    if suffix in _IGNORED_SUFFIXES or suffix not in SUPPORTED_SUFFIXES:
                        continue
                    path = Path(root, name)
                    try:
                        info = os.stat(path, follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        continue
                    relative = path.relative_to(self.input_root).as_posix()
                    found.append((relative, path, info.st_size, info.st_mtime_ns))
        except OSError as exc:
            raise ScannerUnavailable(f"Ошибка обхода INPUT_DIR: {exc}") from exc

        if walk_errors:
            logger.warning("Some input subdirectories are unavailable: %s", "; ".join(walk_errors[:5]))

        latest = self._latest_states([item[0] for item in found])
        now = utc_now()
        candidates: list[ScanCandidate] = []
        skipped = 0

        for relative, path, size, mtime_ns in found:
            state = latest.get(relative)
            if state is not None:
                if (
                    state.status in TERMINAL_STATUSES
                    and state.size_bytes == size
                    and state.source_mtime_ns == mtime_ns
                ):
                    skipped += 1
                    continue
                if state.status == STATUS_PROCESSING:
                    updated = _as_aware(state.updated_at)
                    if updated is None or (now - updated).total_seconds() < self.settings.stale_processing_seconds:
                        skipped += 1
                        continue
                if state.status == STATUS_ERROR:
                    if state.attempts >= self.settings.max_attempts:
                        skipped += 1
                        continue
                    retry_at = _as_aware(state.next_retry_at)
                    if retry_at is not None and retry_at > now:
                        skipped += 1
                        continue
                if state.status == STATUS_PENDING:
                    pass
                is_new = False
            else:
                is_new = True

            candidates.append(
                ScanCandidate(
                    absolute_path=path,
                    relative_path=relative,
                    size=size,
                    mtime_ns=mtime_ns,
                    is_new=is_new,
                )
            )

        return candidates, skipped

    def _latest_states(self, relative_paths: list[str]) -> dict[str, LatestDocumentState]:
        if not relative_paths:
            return {}

        def _operation() -> dict[str, LatestDocumentState]:
            result: dict[str, LatestDocumentState] = {}
            with self.db.session() as session:
                # SQLite has a limit on bound parameters; query in chunks.
                for offset in range(0, len(relative_paths), 400):
                    chunk = relative_paths[offset : offset + 400]
                    rows = session.execute(
                        select(
                            Document.source_path,
                            Document.status,
                            Document.size_bytes,
                            Document.source_mtime_ns,
                            Document.attempts,
                            Document.next_retry_at,
                            Document.updated_at,
                        )
                        .where(Document.source_path.in_(chunk))
                        .order_by(Document.id.desc())
                    ).all()
                    for row in rows:
                        if row.source_path not in result:
                            result[row.source_path] = LatestDocumentState(
                                status=row.status,
                                size_bytes=row.size_bytes,
                                source_mtime_ns=row.source_mtime_ns,
                                attempts=row.attempts,
                                next_retry_at=row.next_retry_at,
                                updated_at=row.updated_at,
                            )
            return result

        try:
            return self.db.run_with_retry(_operation)
        except Exception as exc:
            logger.exception("Could not load processing state from SQLite")
            raise ScannerUnavailable(f"База данных временно недоступна: {exc}") from exc
