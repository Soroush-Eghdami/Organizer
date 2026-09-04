from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

try:
    from core.models import ProposedAction, ActionStatus
    from core.queue_manager import QueueManager
except ImportError:  # when imported as organizer.core.mover
    from .models import ProposedAction, ActionStatus
    from .queue_manager import QueueManager


logger = logging.getLogger(__name__)

# Default activity log location relative to project root (mover is decoupled,
# but we provide a sane default if main.py hasn't configured logging).
# main.py should configure logging.FileHandler to logs/activity.log once;
# mover just uses logger.info — we also do a direct append as fallback
# so tests don't depend on external logging config.
_DEFAULT_ACTIVITY_LOG = Path(__file__).resolve().parent.parent / "logs" / "activity.log"


class Mover:
    """
    Executes APPROVED ProposedActions by actually moving files on disk.

    This is the ONLY module that touches the filesystem for moves —
    that boundary is what let us unit-test classifier.py and
    queue_manager.py without any risk to real files. Keep it that way.

    Design principle: never destroy data. A move that would overwrite
    an existing file, or that fails partway, should be recoverable —
    not silently lose anything.
    """

    def __init__(self, organize_root: str, queue_manager: QueueManager):
        """
        organize_root: base folder that suggested_dest is relative to.
                       e.g. if organize_root = "C:/Organized" and
                       suggested_dest = "Photos", the real destination
                       folder is "C:/Organized/Photos".

                       suggested_dest is a plain string like "Photos" rather
                       than a full path because classifier.py shouldn't need to
                       know WHERE the user's organize folder lives.
                       That's mover.py's concern alone. Good separation.
        """
        self.organize_root = Path(organize_root)
        self.qm = queue_manager

    def _resolve_destination_path(self, action: ProposedAction) -> Path:
        """
        Build the full destination path:
        organize_root / action.suggested_dest / action.filename

        Ensures the destination FOLDER exists before moving
        (Path.mkdir(parents=True, exist_ok=True)) — don't assume it's
        already there.
        """
        # Prefer action.filename (already derived), fallback to basename
        filename = action.filename or Path(action.src_path).name
        # Guard against empty filename (e.g. src_path was a directory or "")
        if not filename:
            filename = Path(action.src_path).name or "unnamed"

        dest_dir = self.organize_root / action.suggested_dest
        # Make folder exist — this is the "folder doesn't exist yet" case from review
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # If we can't create the folder, let caller handle it (shutil.move would fail anyway)
            # but log for diagnostics — don't swallow silently.
            logger.warning("mover: could not create destination folder %s: %s", dest_dir, e)
            # Still return the intended path; move_action will catch the shutil error and set_error
        return dest_dir / filename

    def _resolve_name_collision(self, dest_path: Path) -> Path:
        """
        If dest_path already exists, DO NOT silently overwrite it.
        Find a non-colliding name by appending " (1)", " (2)", etc. before extension:
            report.pdf -> report (1).pdf -> report (2).pdf ...

        Loops until a free path is found. Caps at 1000 attempts so a pathological
        case can't hang forever — raises FileExistsError if cap hit.
        """
        if not dest_path.exists():
            return dest_path

        stem = dest_path.stem
        suffix = dest_path.suffix  # includes dot, or "" if none
        parent = dest_path.parent

        # Try "name (1).ext" .. "name (1000).ext"
        for i in range(1, 1001):
            candidate = parent / f"{stem} ({i}){suffix}"
            if not candidate.exists():
                logger.info("mover: collision resolved %s -> %s", dest_path.name, candidate.name)
                return candidate

        # Cap hit — this is almost certainly a bug or runaway, don't hang
        msg = f"mover: collision loop cap (1000) hit for {dest_path}"
        logger.error(msg)
        raise FileExistsError(msg)

    def _log_activity(self, action: ProposedAction, final_dest: Path) -> None:
        """
        Append a line to logs/activity.log. We lean on Python's logging module
        (configured once in main.py to write to logs/activity.log) — keeps mover
        simple. As fallback, we also ensure a direct append so tests pass even
        without external logging setup.
        """
        msg = f"MOVED {action.src_path} -> {final_dest} [id={action.id} rule={action.matched_rule}]"
        # Primary: use logger (main.py should have FileHandler to activity.log)
        logger.info(msg)

        # Fallback: direct append to default log file if logger has no FileHandler.
        # Check if any handler writes to activity.log; if not, append ourselves.
        # This keeps mover simple but guarantees the log file exists.
        try:
            has_file_handler = False
            for h in logging.getLogger().handlers + logger.handlers:
                # FileHandler has baseFilename
                if hasattr(h, "baseFilename"):
                    try:
                        if Path(getattr(h, "baseFilename")).resolve() == _DEFAULT_ACTIVITY_LOG.resolve():
                            has_file_handler = True
                            break
                    except Exception:
                        pass
            if not has_file_handler:
                _DEFAULT_ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
                with open(_DEFAULT_ACTIVITY_LOG, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
        except Exception:
            # Logging must never break the move
            logger.debug("mover: fallback activity.log append failed", exc_info=True)

    def move_action(self, action: ProposedAction) -> bool:
        """
        Execute a single approved action.

        1. Guard: if action.status != ActionStatus.APPROVED, refuse to
           move (return False, maybe log a warning). Defense-in-depth —
           never trust that caller only passed approved actions.
        2. Guard: if the source file no longer exists, call
           self.qm.set_error(action.id, "source file no longer exists")
           and return False.
        3. Resolve destination path (with collision handling).
        4. shutil.move(...) wrapped in try/except. On ANY exception,
           call self.qm.set_error and return False. NEVER propagate —
           one bad file shouldn't halt the batch.
        5. On success: self.qm.update_status(MOVED) and log to activity.log.
        6. Return True.
        """
        # 1. Defense-in-depth: only APPROVED may be moved
        # Accept both Enum and string (queue may return string-coerced)
        try:
            status = action.status if isinstance(action.status, ActionStatus) else ActionStatus(str(action.status))
        except ValueError:
            status = action.status  # unknown, will fail check below

        if status != ActionStatus.APPROVED:
            msg = f"mover: refusing to move non-approved action {action.id} (status={action.status})"
            logger.warning(msg)
            try:
                self.qm.set_error(action.id, f"refused: status is {action.status}, expected APPROVED")
            except Exception:
                logger.debug("mover: set_error failed for %s", action.id, exc_info=True)
            return False

        # 2. Source existence guard (deleted mid-move, already moved, etc.)
        src = Path(action.src_path)
        if not src.exists():
            msg = "source file no longer exists"
            logger.warning("mover: source missing %s (id=%s)", action.src_path, action.id)
            try:
                self.qm.set_error(action.id, msg)
            except Exception:
                logger.debug("mover: set_error failed", exc_info=True)
            return False
        if src.is_dir():
            msg = "source is a directory, not a file"
            logger.warning("mover: source is directory %s (id=%s)", action.src_path, action.id)
            try:
                self.qm.set_error(action.id, msg)
            except Exception:
                pass
            return False

        # 3. Resolve destination + collision
        try:
            dest_path = self._resolve_destination_path(action)
            final_dest = self._resolve_name_collision(dest_path)
        except Exception as e:
            msg = f"failed to resolve destination: {e}"
            logger.exception("mover: %s for %s", msg, action.src_path)
            try:
                self.qm.set_error(action.id, msg)
            except Exception:
                pass
            return False

        # 4. Move — shutil handles cross-drive copy+delete on Windows
        try:
            # Ensure parent still exists (TOCTOU after _resolve)
            final_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(final_dest))
        except Exception as e:
            msg = f"move failed: {e}"
            logger.exception("mover: %s (%s -> %s) [id=%s]", msg, action.src_path, final_dest, action.id)
            try:
                self.qm.set_error(action.id, msg)
            except Exception:
                logger.debug("mover: set_error failed", exc_info=True)
            return False

        # 5. Success: update queue + log
        try:
            self.qm.update_status(action.id, ActionStatus.MOVED)
        except Exception:
            logger.exception("mover: update_status to MOVED failed for %s", action.id)
            # File was already moved — don't return False, but still log activity
            # The DB may be out of sync, but the filesystem move succeeded.
        try:
            self._log_activity(action, final_dest)
        except Exception:
            logger.exception("mover: activity log failed for %s", action.id)

        return True

    def move_all_approved(self) -> dict:
        """
        Batch version: fetch all APPROVED items from the queue and move
        each one via move_action().

        Returns a summary dict, e.g. {"moved": 4, "failed": 1}.
        This is what cli_app.py / gui_app.py call after the user
        approves a batch.
        """
        try:
            approved = self.qm.list_by_status(ActionStatus.APPROVED)
        except Exception as e:
            logger.exception("mover: failed to fetch approved items: %s", e)
            return {"moved": 0, "failed": 0, "error": str(e)}

        moved = 0
        failed = 0
        for action in approved:
            try:
                ok = self.move_action(action)
                if ok:
                    moved += 1
                else:
                    failed += 1
            except Exception:
                # Extra safety — move_action itself shouldn't raise, but if it does, count as failed
                logger.exception("mover: unexpected exception for %s", getattr(action, 'id', '?'))
                failed += 1
                try:
                    self.qm.set_error(getattr(action, 'id', ''), "unexpected mover exception")
                except Exception:
                    pass

        summary = {"moved": moved, "failed": failed}
        logger.info("mover: batch complete %s", summary)
        return summary
