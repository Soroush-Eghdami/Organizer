from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Dict

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
except ImportError:
    Observer = None  # type: ignore
    class FileSystemEventHandler:  # type: ignore
        pass
    class FileSystemEvent:  # type: ignore
        is_directory: bool = False
        src_path: str = ""
        dest_path: str = ""

try:
    from core.models import FileEvent
except ImportError:
    from .models import FileEvent

logger = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    """Watchdog handler with restart-timer debounce. Runs on observer thread."""

    def __init__(self, on_settled: Callable[[str], None], debounce_seconds: float = 2.0, settle_check_interval: float = 0.5):
        super().__init__()
        self.on_settled = on_settled
        self.debounce_seconds = debounce_seconds
        self.settle_check_interval = settle_check_interval
        self._lock = threading.Lock()
        self._pending: Dict[str, threading.Timer] = {}

    def _schedule_settle_check(self, path: str) -> None:
        """Start/restart timer for path. Resets on rapid on_modified."""
        if not path:
            return
        path = os.path.normpath(path)
        with self._lock:
            old = self._pending.get(path)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass
            timer = threading.Timer(self.debounce_seconds, self._check_settled, args=(path,))
            timer.daemon = True
            self._pending[path] = timer
            timer.start()

    def _check_settled(self, path: str) -> None:
        """After quiet period, verify file exists + size stable, then fire callback."""
        with self._lock:
            self._pending.pop(path, None)
        try:
            if not os.path.exists(path):
                return
            if self.settle_check_interval > 0:
                try:
                    s1 = os.path.getsize(path) if os.path.isfile(path) else -1
                    time.sleep(self.settle_check_interval)
                    if not os.path.exists(path):
                        return
                    s2 = os.path.getsize(path) if os.path.isfile(path) else -1
                    if s1 != s2:
                        self._schedule_settle_check(path)
                        return
                except OSError:
                    pass
        except Exception:
            return
        try:
            self.on_settled(path)
        except Exception:
            logger.exception("watcher: on_settled failed for %s", path)

    def cancel_all(self) -> int:
        """Cancel all timers. Returns count."""
        with self._lock:
            n = len(self._pending)
            for t in list(self._pending.values()):
                try:
                    t.cancel()
                except Exception:
                    pass
            self._pending.clear()
            return n

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def on_created(self, event: FileSystemEvent) -> None:
        if not getattr(event, "is_directory", False):
            self._schedule_settle_check(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not getattr(event, "is_directory", False):
            self._schedule_settle_check(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not getattr(event, "is_directory", False):
            dest = getattr(event, "dest_path", None) or getattr(event, "src_path", "")
            self._schedule_settle_check(dest)

    def on_closed(self, event: FileSystemEvent) -> None:
        if not getattr(event, "is_directory", False):
            self._schedule_settle_check(event.src_path)


class FolderWatcher:
    """Public API: start()/stop() around watchdog Observer + debounced handler."""

    def __init__(self, watch_path: str, on_file_ready: Callable[[FileEvent], None], recursive: bool = False, debounce_seconds: float = 2.0):
        self.watch_path = str(Path(watch_path).resolve())
        self.on_file_ready = on_file_ready
        self.recursive = recursive
        self._handler = _DebouncedHandler(on_settled=self._handle_settled_path, debounce_seconds=debounce_seconds)
        self._observer: Optional[Observer] = None  # type: ignore
        self._started = False

    def _handle_settled_path(self, path: str) -> None:
        """Build FileEvent and call on_file_ready. Never let exceptions kill observer."""
        try:
            event = FileEvent.from_path(path)
        except Exception:
            logger.exception("watcher: FileEvent failed for %s", path)
            return
        try:
            self.on_file_ready(event)
        except Exception:
            logger.exception("watcher: on_file_ready failed for %s", path)

    def start(self) -> None:
        """Start observer thread. Non-blocking."""
        if self._started and self._observer is not None:
            return
        if Observer is None:
            raise RuntimeError("watchdog not installed: pip install watchdog")
        p = Path(self.watch_path)
        if not p.exists():
            raise FileNotFoundError(f"watch_path not found: {self.watch_path}")
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {self.watch_path}")
        observer = Observer()
        observer.schedule(self._handler, self.watch_path, recursive=self.recursive)
        observer.start()
        self._observer = observer
        self._started = True
        logger.info("FolderWatcher started %s (recursive=%s)", self.watch_path, self.recursive)

    def stop(self) -> int:
        """Stop observer + cancel timers. Returns cancelled count for warning."""
        cancelled = 0
        try:
            cancelled = self._handler.cancel_all()
            if cancelled:
                logger.warning("FolderWatcher: %d still settling in %s — will scan next start", cancelled, self.watch_path)
        except Exception:
            logger.exception("watcher: cancel failed")
        obs = self._observer
        if obs is not None:
            try:
                obs.stop()
            except Exception:
                pass
            try:
                obs.join(timeout=5)
            except Exception:
                pass
            self._observer = None
            self._started = False
            logger.info("FolderWatcher stopped %s", self.watch_path)
        else:
            self._started = False
        return cancelled

    def is_running(self) -> bool:
        obs = self._observer
        return bool(obs is not None and getattr(obs, "is_alive", lambda: False)())

    def __enter__(self) -> "FolderWatcher":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
