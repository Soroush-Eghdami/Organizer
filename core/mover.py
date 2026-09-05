from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

try:
    from core.models import ProposedAction, ActionStatus
    from core.queue_manager import QueueManager
except ImportError:
    from .models import ProposedAction, ActionStatus
    from .queue_manager import QueueManager

try:
    from core.paths import get_app_dir
except ImportError:
    from .paths import get_app_dir

logger = logging.getLogger(__name__)
_DEFAULT_ACTIVITY_LOG = get_app_dir() / "logs" / "activity.log"


class Mover:
    """Moves only APPROVED items. Sole place that touches filesystem for moves."""

    def __init__(self, organize_root: str, queue_manager: QueueManager):
        self.organize_root = Path(organize_root)
        self.qm = queue_manager

    def _resolve_destination_path(self, action: ProposedAction) -> Path:
        """organize_root / suggested_dest / filename, ensuring folder exists."""
        filename = action.filename or Path(action.src_path).name or "unnamed"
        dest_dir = self.organize_root / action.suggested_dest
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("mover: mkdir %s failed: %s", dest_dir, e)
        return dest_dir / filename

    def _resolve_name_collision(self, dest_path: Path) -> Path:
        """Avoid overwrite: foo.pdf -> foo (1).pdf ... up to 1000."""
        if not dest_path.exists():
            return dest_path
        stem, suffix, parent = dest_path.stem, dest_path.suffix, dest_path.parent
        for i in range(1, 1001):
            cand = parent / f"{stem} ({i}){suffix}"
            if not cand.exists():
                logger.info("mover: collision %s -> %s", dest_path.name, cand.name)
                return cand
        msg = f"collision cap hit for {dest_path}"
        logger.error(msg)
        raise FileExistsError(msg)

    def _log_activity(self, action: ProposedAction, final_dest: Path) -> None:
        """Log to activity.log via logger + fallback direct append."""
        msg = f"MOVED {action.src_path} -> {final_dest} [id={action.id} rule={action.matched_rule}]"
        logger.info(msg)
        try:
            has_handler = any(hasattr(h, "baseFilename") and Path(getattr(h, "baseFilename")).resolve() == _DEFAULT_ACTIVITY_LOG.resolve() for h in logging.getLogger().handlers + logger.handlers if hasattr(h, "baseFilename"))
            if not has_handler:
                _DEFAULT_ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
                with open(_DEFAULT_ACTIVITY_LOG, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
        except Exception:
            logger.debug("mover: log append failed", exc_info=True)

    def move_action(self, action: ProposedAction) -> bool:
        """Move one APPROVED item. Returns True on success."""
        # 1. Only APPROVED
        try:
            status = action.status if isinstance(action.status, ActionStatus) else ActionStatus(str(action.status))
        except ValueError:
            status = action.status  # type: ignore
        if status != ActionStatus.APPROVED:
            logger.warning("mover: refuse non-approved %s (%s)", action.id, action.status)
            try:
                self.qm.set_error(action.id, f"refused: {action.status} != APPROVED")
            except Exception:
                pass
            return False

        # 2. Source must exist and be a file
        src = Path(action.src_path)
        if not src.exists():
            logger.warning("mover: missing %s (%s)", action.src_path, action.id)
            try:
                self.qm.set_error(action.id, "source file no longer exists")
            except Exception:
                pass
            return False
        if src.is_dir():
            logger.warning("mover: source is dir %s", action.src_path)
            try:
                self.qm.set_error(action.id, "source is a directory")
            except Exception:
                pass
            return False

        # 3. Destination + collision
        try:
            final_dest = self._resolve_name_collision(self._resolve_destination_path(action))
        except Exception as e:
            msg = f"resolve failed: {e}"
            logger.exception("mover: %s", msg)
            try:
                self.qm.set_error(action.id, msg)
            except Exception:
                pass
            return False

        # 4. Move (handles cross-drive)
        try:
            final_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(final_dest))
        except Exception as e:
            msg = f"move failed: {e}"
            logger.exception("mover: %s", msg)
            try:
                self.qm.set_error(action.id, msg)
            except Exception:
                pass
            return False

        # 5. Mark moved + log
        try:
            self.qm.update_status(action.id, ActionStatus.MOVED)
        except Exception:
            logger.exception("mover: update_status failed %s", action.id)
        try:
            self._log_activity(action, final_dest)
        except Exception:
            logger.exception("mover: log failed %s", action.id)
        return True

    def move_all_approved(self) -> dict:
        """Move all APPROVED. Returns {moved, failed}."""
        try:
            approved = self.qm.list_by_status(ActionStatus.APPROVED)
        except Exception as e:
            logger.exception("mover: fetch approved failed: %s", e)
            return {"moved": 0, "failed": 0, "error": str(e)}
        moved = failed = 0
        for a in approved:
            try:
                if self.move_action(a):
                    moved += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("mover: unexpected %s", getattr(a, "id", "?"))
                failed += 1
                try:
                    self.qm.set_error(getattr(a, "id", ""), "unexpected mover exception")
                except Exception:
                    pass
        summary = {"moved": moved, "failed": failed}
        logger.info("mover batch %s", summary)
        return summary
