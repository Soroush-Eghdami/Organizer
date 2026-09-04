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
except ImportError:  # graceful fallback for test envs without watchdog installed
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
    """
    Low-level watchdog handler. Does NOT call the classifier directly —
    it just tracks "this path changed recently" and schedules a settle
    check. Keeping this dumb (like FileEvent) makes it testable without
    needing a real classifier/queue wired up.

    KEY CONCURRENCY FACT: watchdog calls on_created/on_modified/etc from
    ITS OWN background thread (the Observer's thread), not your main
    thread. Any shared state you touch here (like a dict of pending
    timers) needs a lock, or you'll get race conditions if two events
    fire close together.
    """

    def __init__(
        self,
        on_settled: Callable[[str], None],
        debounce_seconds: float = 2.0,
        settle_check_interval: float = 0.5,
    ):
        """
        on_settled: callback invoked (path: str) once a file has stopped
                    changing for `debounce_seconds`. This is what
                    watcher.py's public class will hook up to the
                    classifier.
        debounce_seconds: how long a file's size must be unchanged
                    before we consider it "done being written".
        settle_check_interval: how often to poll file size while waiting.
        """
        super().__init__()
        self.on_settled = on_settled
        self.debounce_seconds = debounce_seconds
        self.settle_check_interval = settle_check_interval

        self._lock = threading.Lock()
        self._pending: Dict[str, threading.Timer] = {}

    def _schedule_settle_check(self, path: str) -> None:
        """
        Restart-the-timer debounce pattern:
        1. Acquire the lock.
        2. If there's already a pending Timer for this path, cancel it
           (the file changed again — reset the clock).
        3. Start a new threading.Timer(self.debounce_seconds, ...) that
           calls self._check_settled(path) after the delay.
        4. Store the new Timer in self._pending[path].
        5. Release the lock.

        Why cancel-and-restart instead of just letting old timers fire?
        Because a file still being written keeps triggering on_modified
        — you want the "quiet period" to restart each time, not fire
        after a fixed 2 seconds regardless of ongoing writes.
        """
        if not path:
            return
        # Normalize path to avoid ./ vs absolute duplicates
        path = os.path.normpath(path)
        with self._lock:
            old = self._pending.get(path)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass

            timer = threading.Timer(self.debounce_seconds, self._check_settled, args=(path,))
            timer.daemon = True  # don't block interpreter exit
            self._pending[path] = timer
            timer.start()

    def _check_settled(self, path: str) -> None:
        """
        Called after the debounce delay with no further modifications.
        1. Remove this path from self._pending (under the lock).
        2. Verify the file still exists (it might've been deleted or
           moved already — don't crash if os.path.exists is False).
        3. Call self.on_settled(path).

        Optional stricter check (large file gap): re-check file size twice
        `settle_check_interval` apart before declaring settled. Simple
        event-based version is fine for v1.
        """
        # 1. Remove from pending
        with self._lock:
            self._pending.pop(path, None)

        # 2. Verify file still exists
        try:
            if not os.path.exists(path):
                logger.debug("watcher: file vanished before settle: %s", path)
                return
            # Strict settle: ensure size stable over settle_check_interval
            # This catches large copies with gaps > debounce window.
            # Keep it cheap — only if settle_check_interval > 0 and file exists
            if self.settle_check_interval > 0:
                try:
                    size1 = os.path.getsize(path) if os.path.isfile(path) else -1
                    time.sleep(self.settle_check_interval)
                    # Re-check existence after sleep
                    if not os.path.exists(path):
                        return
                    size2 = os.path.getsize(path) if os.path.isfile(path) else -1
                    if size1 != size2:
                        # Still being written — restart debounce
                        logger.debug("watcher: file size changed during settle, re-scheduling %s", path)
                        self._schedule_settle_check(path)
                        return
                except OSError:
                    # Permission / race — treat as not settled yet? just try once more
                    pass
        except Exception as e:
            logger.debug("watcher: exists check failed for %s: %s", path, e)
            return

        # 3. Fire callback — never let exception kill the Timer thread
        try:
            self.on_settled(path)
        except Exception:
            logger.exception("watcher: on_settled callback failed for %s", path)

    def cancel_all(self) -> int:
        """Cancel all in-flight timers (called on FolderWatcher.stop()). Returns count cancelled."""
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

    # ------------------------------------------------------------------ #
    # watchdog event hooks — these run on the Observer's thread
    # ------------------------------------------------------------------ #
    def on_created(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        self._schedule_settle_check(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        self._schedule_settle_check(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # A rename/move INTO the watched folder also needs classifying.
        if getattr(event, "is_directory", False):
            return
        # watchdog: event.dest_path is the new location
        dest = getattr(event, "dest_path", None) or getattr(event, "src_path", "")
        self._schedule_settle_check(dest)

    # Also handle created via move on some platforms firing as on_created with nested dirs
    def on_closed(self, event: FileSystemEvent) -> None:
        # Some editors emit close_write — treat like modified for safety
        if getattr(event, "is_directory", False):
            return
        self._schedule_settle_check(event.src_path)


class FolderWatcher:
    """
    Public interface — this is what main.py / cli_app.py / gui_app.py
    actually import and use. Wraps the watchdog Observer + debounced
    handler behind a simple start()/stop() API.
    """

    def __init__(
        self,
        watch_path: str,
        on_file_ready: Callable[[FileEvent], None],
        recursive: bool = False,
        debounce_seconds: float = 2.0,
    ):
        """
        watch_path: folder to monitor.
        on_file_ready: callback invoked with a fully-built FileEvent
                       once a file has settled. This is where
                       classify_and_queue() gets called from — but
                       NOTE: watcher.py should not import Classifier
                       directly. Keep them decoupled; whoever
                       constructs FolderWatcher (main.py) passes in
                       the callback that bridges to the classifier.
        """
        self.watch_path = str(Path(watch_path).resolve())
        self.on_file_ready = on_file_ready
        self.recursive = recursive

        self._handler = _DebouncedHandler(
            on_settled=self._handle_settled_path,
            debounce_seconds=debounce_seconds,
        )
        self._observer: Optional[Observer] = None  # type: ignore
        self._started = False

    def _handle_settled_path(self, path: str) -> None:
        """
        Bridges the low-level handler to the public callback.
        Builds a FileEvent.from_path(path) and calls
        self.on_file_ready(event). Swallows/logs exceptions so one bad
        event doesn't kill the Observer thread.
        """
        try:
            event = FileEvent.from_path(path)
        except Exception as e:
            logger.exception("watcher: failed to build FileEvent for %s: %s", path, e)
            return

        try:
            self.on_file_ready(event)
        except Exception:
            logger.exception("watcher: on_file_ready callback failed for %s", path)

    def start(self) -> None:
        """
        1. Create self._observer = Observer()
        2. observer.schedule(self._handler, self.watch_path, recursive=self.recursive)
        3. observer.start()

        This spawns the Observer's own thread. start() returns immediately —
        it does NOT block. The caller is responsible for keeping the main
        thread alive for as long as watching should continue.
        """
        if self._started and self._observer is not None:
            logger.debug("FolderWatcher already started")
            return

        if Observer is None:
            raise RuntimeError(
                "watchdog is not installed. Install with: pip install watchdog"
            )

        # Ensure watch path exists
        p = Path(self.watch_path)
        if not p.exists():
            raise FileNotFoundError(f"watch_path does not exist: {self.watch_path}")
        if not p.is_dir():
            raise NotADirectoryError(f"watch_path is not a directory: {self.watch_path}")

        observer = Observer()
        observer.schedule(self._handler, self.watch_path, recursive=self.recursive)
        observer.start()
        self._observer = observer
        self._started = True
        logger.info("FolderWatcher started on %s (recursive=%s)", self.watch_path, self.recursive)

    def stop(self) -> int:
        """
        1. If self._observer exists: observer.stop(), then observer.join()
            (join() waits for the thread to actually finish — don't skip
            this, or you can get a half-dead observer on fast app exit).
        2. Cancel any in-flight debounce timers in self._handler._pending,
            so they don't fire after stop().
        Returns count of cancelled settling timers (0 if none). Caller can
        warn the user if files were still settling at shutdown.
        """
        # Cancel debounce timers first so they don't fire after stop()
        cancelled = 0
        try:
            cancelled = self._handler.cancel_all()
            if cancelled:
                logger.warning(
                    "FolderWatcher: %d file(s) were still settling and were not queued — "
                    "they remain in %s and will be caught on next watch startup scan",
                    cancelled,
                    self.watch_path,
                )
        except Exception:
            logger.exception("watcher: failed to cancel debounce timers")

        obs = self._observer
        if obs is not None:
            try:
                obs.stop()
            except Exception:
                logger.exception("watcher: observer.stop() failed")
            try:
                # join with timeout so we don't hang forever on broken observer
                obs.join(timeout=5)
            except Exception:
                logger.exception("watcher: observer.join() failed")
            finally:
                self._observer = None
                self._started = False
                logger.info("FolderWatcher stopped on %s", self.watch_path)
        else:
            self._started = False
        return cancelled

    def is_running(self) -> bool:
        """Return True if the observer thread is alive."""
        obs = self._observer
        return bool(obs is not None and getattr(obs, "is_alive", lambda: False)())

    def __enter__(self) -> "FolderWatcher":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
