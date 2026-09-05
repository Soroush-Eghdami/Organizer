from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
import os
import uuid
from typing import Optional, Any, Dict


class ActionStatus(str, Enum):
    """Queue item status."""

    PENDING = "pending"
    APPROVED = "approved"
    MOVED = "moved"
    REJECTED = "rejected"


@dataclass
class FileEvent:
    """Raw filesystem event from watcher. Input to classifier. No logic here."""

    src_path: str
    filename: str = ""
    extension: str = ""
    file_size: Optional[int] = None
    is_directory: bool = False
    event_type: str = "created"
    detected_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        # Fill derivable fields so classifier avoids extra I/O
        if not self.filename:
            self.filename = os.path.basename(self.src_path)
        if not self.extension:
            self.extension = Path(self.src_path).suffix.lower()
        # Best-effort file info, never fail
        try:
            if self.file_size is None and self.src_path and os.path.exists(self.src_path):
                self.file_size = os.path.getsize(self.src_path) if os.path.isfile(self.src_path) else None
            if not self.is_directory and self.src_path and os.path.exists(self.src_path):
                self.is_directory = os.path.isdir(self.src_path)
        except OSError:
            pass

    @property
    def stem(self) -> str:
        return Path(self.filename).stem if self.filename else Path(self.src_path).stem

    @property
    def directory(self) -> str:
        return str(Path(self.src_path).parent)

    @classmethod
    def from_path(cls, path: str | os.PathLike[str], event_type: str = "created", detected_at: Optional[datetime] = None) -> FileEvent:
        """Helper for watcher.py."""
        return cls(src_path=str(path), event_type=event_type, detected_at=detected_at or datetime.now())

    def to_dict(self) -> Dict[str, Any]:
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
    """Classifier suggestion shown in CLI/GUI and stored in SQLite. Central type."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    src_path: str = ""
    suggested_dest: str = ""
    matched_rule: str = ""  # e.g. "extension:.jpg -> Photos"
    confidence: float = 1.0  # reserved for v2
    status: ActionStatus = ActionStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = None
    filename: str = ""
    extension: str = ""
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            try:
                self.status = ActionStatus(self.status)
            except ValueError:
                self.status = ActionStatus.PENDING
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError):
            self.confidence = 1.0
        self.confidence = max(0.0, min(1.0, self.confidence))
        if not self.filename and self.src_path:
            self.filename = os.path.basename(self.src_path)
        if not self.extension and self.src_path:
            self.extension = Path(self.src_path).suffix.lower()

    @property
    def is_pending(self) -> bool:
        return self.status == ActionStatus.PENDING

    @property
    def is_resolved(self) -> bool:
        return self.status in (ActionStatus.APPROVED, ActionStatus.REJECTED, ActionStatus.MOVED)

    def approve(self) -> None:
        self.status = ActionStatus.APPROVED
        self.resolved_at = datetime.now()

    def reject(self) -> None:
        self.status = ActionStatus.REJECTED
        self.resolved_at = datetime.now()

    def mark_moved(self) -> None:
        self.status = ActionStatus.MOVED
        self.resolved_at = self.resolved_at or datetime.now()

    def mark_failed(self, message: str) -> None:
        self.error_message = message

    def to_dict(self) -> Dict[str, Any]:
        """For SQLite/JSON. Datetimes -> ISO, Enum -> value."""
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
        """Rebuild from to_dict() / DB row."""
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
    def from_file_event(cls, event: FileEvent, suggested_dest: str, matched_rule: str = "", confidence: float = 1.0) -> ProposedAction:
        """Map FileEvent -> ProposedAction. Keeps classifier thin."""
        return cls(
            src_path=event.src_path,
            suggested_dest=suggested_dest,
            matched_rule=matched_rule,
            confidence=confidence,
            filename=event.filename,
            extension=event.extension,
        )
