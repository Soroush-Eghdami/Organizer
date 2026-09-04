from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
import os
import uuid
from typing import Optional, Any, Dict


class ActionStatus(str, Enum):
    """Status of the proposed file action in the queue."""

    PENDING = "pending"
    APPROVED = "approved"
    MOVED = "moved"
    REJECTED = "rejected"


@dataclass
class FileEvent:
    """
    Represents a raw filesystem event captured by watcher.py,
    BEFORE any classification has happened.

    This is the watcher's output and the classifier's input.
    Keep it dumb — no classification logic belongs here.

    The watcher populates this with cheap, derivable metadata so the
    classifier doesn't need to hit the filesystem again.
    """

    src_path: str
    filename: str = ""
    extension: str = ""
    file_size: Optional[int] = None
    is_directory: bool = False
    event_type: str = "created"  # created | modified | moved
    detected_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        # Derive filename / extension from src_path if not explicitly given.
        # Keep original src_path as-is (don't resolve) — watcher owns path format.
        if not self.filename:
            self.filename = os.path.basename(self.src_path)

        if not self.extension:
            # lower-cased extension with leading dot, e.g. ".jpg"
            # empty string if no extension
            self.extension = Path(self.src_path).suffix.lower()

        # Try to populate file_size / is_directory only if path exists
        # and caller didn't provide values. Never raise here — watcher must
        # stay dumb and non-blocking.
        try:
            if self.file_size is None and self.src_path and os.path.exists(self.src_path):
                if os.path.isfile(self.src_path):
                    self.file_size = os.path.getsize(self.src_path)
                else:
                    self.file_size = None
            if not self.is_directory and self.src_path and os.path.exists(self.src_path):
                self.is_directory = os.path.isdir(self.src_path)
        except OSError:
            # Permission errors / race conditions — leave defaults
            pass

    @property
    def stem(self) -> str:
        """Filename without extension."""
        return Path(self.filename).stem if self.filename else Path(self.src_path).stem

    @property
    def directory(self) -> str:
        """Parent directory of src_path."""
        return str(Path(self.src_path).parent)

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        event_type: str = "created",
        detected_at: Optional[datetime] = None,
    ) -> FileEvent:
        """Convenience factory used by watcher.py."""
        p = str(path)
        return cls(
            src_path=p,
            event_type=event_type,
            detected_at=detected_at or datetime.now(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging / debugging."""
        return {
            "src_path": self.src_path,
            "filename": self.filename,
            "extension": self.extension,
            "file_size": self.file_size,
            "is_directory": self.is_directory,
            "event_type": self.event_type,
            "detected_at": self.detected_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FileEvent:
        """Deserialize from dict produced by to_dict()."""
        detected_at = data.get("detected_at")
        if isinstance(detected_at, str):
            try:
                detected_at = datetime.fromisoformat(detected_at)
            except ValueError:
                detected_at = datetime.now()
        return cls(
            src_path=data["src_path"],
            filename=data.get("filename", ""),
            extension=data.get("extension", ""),
            file_size=data.get("file_size"),
            is_directory=bool(data.get("is_directory", False)),
            event_type=data.get("event_type", "created"),
            detected_at=detected_at or datetime.now(),
        )


@dataclass
class ProposedAction:
    """
    Represents a classifier's *suggestion* for what to do with a
    file. This is what gets stored in the SQLite queue and shown
    to the user in CLI/GUI for confirmation.

    This is the CENTRAL data structure of the whole app — almost
    every module touches this. Get its fields right and everything
    downstream gets easier.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    src_path: str = ""
    suggested_dest: str = ""
    matched_rule: str = ""  # rule identifier, e.g. "extension:.jpg -> Photos"
    confidence: float = 1.0  # unused in Tier 1 (always 1.0) but kept for v2
    status: ActionStatus = ActionStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = None
    # extra metadata useful for UI / logging without schema migration
    filename: str = ""
    extension: str = ""
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        # Coerce string status (from SQLite) into Enum
        if isinstance(self.status, str):
            try:
                self.status = ActionStatus(self.status)
            except ValueError:
                self.status = ActionStatus.PENDING

        # Clamp confidence to [0, 1]
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError):
            self.confidence = 1.0
        self.confidence = max(0.0, min(1.0, self.confidence))

        # Derive filename/extension if not provided (avoid recompute elsewhere)
        if not self.filename and self.src_path:
            self.filename = os.path.basename(self.src_path)
        if not self.extension and self.src_path:
            self.extension = Path(self.src_path).suffix.lower()

        # resolved_at must be None while pending
        if self.status == ActionStatus.PENDING:
            # keep whatever was loaded from DB, but default to None
            pass

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #
    @property
    def is_pending(self) -> bool:
        return self.status == ActionStatus.PENDING

    @property
    def is_resolved(self) -> bool:
        return self.status in (ActionStatus.APPROVED, ActionStatus.REJECTED, ActionStatus.MOVED)

    def approve(self) -> None:
        """Mark as approved by user (awaiting move)."""
        self.status = ActionStatus.APPROVED
        self.resolved_at = datetime.now()

    def reject(self) -> None:
        """Mark as rejected by user."""
        self.status = ActionStatus.REJECTED
        self.resolved_at = datetime.now()

    def mark_moved(self) -> None:
        """Mark as successfully moved on disk (called by mover.py)."""
        self.status = ActionStatus.MOVED
        self.resolved_at = self.resolved_at or datetime.now()

    def mark_failed(self, message: str) -> None:
        """Attach an error message when move fails."""
        self.error_message = message

    # ------------------------------------------------------------------ #
    # SQLite serialization — owned here so every caller is consistent
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to a plain dict suitable for sqlite3 / json.
        Datetimes are stored as ISO-8601 strings, Enum as its value.
        """
        return {
            "id": self.id,
            "src_path": self.src_path,
            "suggested_dest": self.suggested_dest,
            "matched_rule": self.matched_rule,
            "confidence": self.confidence,
            "status": self.status.value if isinstance(self.status, ActionStatus) else str(self.status),
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else str(self.created_at),
            "resolved_at": self.resolved_at.isoformat() if isinstance(self.resolved_at, datetime) else None,
            "filename": self.filename,
            "extension": self.extension,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProposedAction:
        """
        Re-create an instance from a dict produced by to_dict() or a
        raw SQLite row dict. Handles ISO-string -> datetime and string -> Enum.
        """
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                created_at = datetime.now()
        elif not isinstance(created_at, datetime):
            created_at = datetime.now()

        resolved_at = data.get("resolved_at")
        if isinstance(resolved_at, str):
            try:
                resolved_at = datetime.fromisoformat(resolved_at)
            except ValueError:
                resolved_at = None
        elif not isinstance(resolved_at, (datetime, type(None))):
            resolved_at = None

        status = data.get("status", ActionStatus.PENDING)
        if isinstance(status, str):
            try:
                status = ActionStatus(status)
            except ValueError:
                status = ActionStatus.PENDING

        return cls(
            id=str(data.get("id", str(uuid.uuid4()))),
            src_path=data.get("src_path", ""),
            suggested_dest=data.get("suggested_dest", ""),
            matched_rule=data.get("matched_rule", ""),
            confidence=float(data.get("confidence", 1.0)),
            status=status,
            created_at=created_at,
            resolved_at=resolved_at,
            filename=data.get("filename", ""),
            extension=data.get("extension", ""),
            error_message=data.get("error_message"),
        )

    @classmethod
    def from_file_event(
        cls,
        event: FileEvent,
        suggested_dest: str,
        matched_rule: str = "",
        confidence: float = 1.0,
    ) -> ProposedAction:
        """
        Factory used by classifier.py to map a FileEvent -> ProposedAction.
        Keeps classifier thin and ensures field mapping is centralized.
        """
        return cls(
            src_path=event.src_path,
            suggested_dest=suggested_dest,
            matched_rule=matched_rule,
            confidence=confidence,
            filename=event.filename,
            extension=event.extension,
        )