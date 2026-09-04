from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Any

try:
    from core.models import FileEvent, ProposedAction
except ImportError:
    from .models import FileEvent, ProposedAction

# Hardcoded fallback — used if rules.json omits default_destination.
# Keeps the app usable even with a minimal rules file, and guarantees
# that every file gets queued somewhere instead of being silently ignored.
HARDCODED_FALLBACK = "Unsorted"


class Classifier:
    """
    Turns a raw FileEvent into a ProposedAction using rules.json.

    - Extension matching is case-insensitive and dot-normalized.
    - Unmatched files (e.g. .exe, .zip) are routed to `default_destination`
      (from rules.json) or HARDCODED_FALLBACK ("Unsorted") — they are
      queued for review, never silently ignored.
    - Duplicate pending protection is handled SOLELY by
      QueueManager.add(dedup=True). classify() is pure: event in,
      suggestion out, no DB I/O or mutation.
    """

    def __init__(
        self,
        rules_path: str | os.PathLike[str] | None = None,
        default_destination: Optional[str] = None,
        queue_manager: Optional[Any] = None,
    ):
        # Resolve rules.json — default to <organizer>/config/rules.json
        if rules_path is None:
            # core/classifier.py -> organizer/config/rules.json
            here = Path(__file__).resolve().parent
            candidate = here.parent / "config" / "rules.json"
            rules_path = str(candidate) if candidate.exists() else "config/rules.json"

        self.rules_path = str(rules_path)
        self._queue_manager = queue_manager
        # Allow caller to override fallback; otherwise load from JSON
        self._explicit_fallback = default_destination
        self.default_destination: str = HARDCODED_FALLBACK
        self._ext_map: Dict[str, str] = {}  # ".jpg" -> "Photos"
        self._raw_rules: list[dict] = []
        self._load_rules()

    # ------------------------------------------------------------------ #
    # Rules loading
    # ------------------------------------------------------------------ #
    def _load_rules(self) -> None:
        try:
            with open(self.rules_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            # No file — use hardcoded fallback for everything
            self.default_destination = self._explicit_fallback or HARDCODED_FALLBACK
            self._ext_map = {}
            self._raw_rules = []
            return
        except json.JSONDecodeError:
            # Corrupt file — same fallback
            self.default_destination = self._explicit_fallback or HARDCODED_FALLBACK
            self._ext_map = {}
            self._raw_rules = []
            return

        # default_destination: explicit ctor arg wins, then JSON key, then hardcoded
        json_fallback = data.get("default_destination")
        if self._explicit_fallback is not None:
            self.default_destination = self._explicit_fallback
        elif isinstance(json_fallback, str) and json_fallback.strip():
            self.default_destination = json_fallback.strip()
        else:
            self.default_destination = HARDCODED_FALLBACK

        rules = data.get("rules", [])
        self._raw_rules = rules if isinstance(rules, list) else []
        ext_map: Dict[str, str] = {}
        for rule in self._raw_rules:
            if not isinstance(rule, dict):
                continue
            dest = rule.get("destination")
            match = rule.get("match", {})
            exts = match.get("extension") if isinstance(match, dict) else None
            if not dest or not exts:
                continue
            if isinstance(exts, str):
                exts = [exts]
            for ext in exts:
                if not isinstance(ext, str):
                    continue
                norm = ext.strip().lower()
                if not norm.startswith("."):
                    norm = "." + norm
                # First rule wins for overlapping extensions
                if norm not in ext_map:
                    ext_map[norm] = str(dest)
        self._ext_map = ext_map

    def reload(self) -> None:
        """Reload rules from disk (useful if user edits rules.json at runtime)."""
        self._load_rules()

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    def classify(self, event: FileEvent) -> Optional[ProposedAction]:
        """
        Pure function: FileEvent in, ProposedAction out.

        - No DB access, no mutation of persisted state.
        - Returns None for directories or empty paths.
        - Unmatched extensions go to default_destination (Unsorted) — never
          silently ignored.

        Duplicate suppression lives in QueueManager.add(dedup=True) /
        classify_and_queue(), not here.
        """
        if not event or not event.src_path:
            return None
        if event.is_directory:
            return None

        ext = (event.extension or Path(event.src_path).suffix).lower()
        if not ext.startswith(".") and ext:
            ext = "." + ext

        dest = self._ext_map.get(ext)
        if dest:
            matched_rule = f"extension:{ext} -> {dest}"
            suggested_dest = dest
        else:
            # Fallback bucket for .exe, .zip, unknown, or no extension
            suggested_dest = self.default_destination
            if ext:
                matched_rule = f"extension:{ext} -> {suggested_dest} (fallback)"
            else:
                matched_rule = f"no-extension -> {suggested_dest} (fallback)"

        return ProposedAction.from_file_event(
            event=event,
            suggested_dest=suggested_dest,
            matched_rule=matched_rule,
            confidence=1.0,
        )

    def classify_and_queue(
        self, event: FileEvent, queue_manager: Optional[Any] = None
    ) -> Optional[ProposedAction]:
        """
        Convenience: classify and directly insert into the queue.
        Uses QueueManager.add(dedup=True) so watcher debounce firing twice
        does not create two pending rows for the same file.
        Returns the inserted (or existing pending) action, or None if skipped.
        """
        qm = queue_manager or self._queue_manager
        action = self.classify(event)
        if action is None or qm is None:
            return action
        qm.add(action, dedup=True)
        # If dedup kicked in, fetch the canonical pending row
        existing = qm.find_pending_by_path(action.src_path)
        return existing or action

    # ------------------------------------------------------------------ #
    # Convenience helpers
    # ------------------------------------------------------------------ #
    @property
    def extension_map(self) -> Dict[str, str]:
        return dict(self._ext_map)

    def __repr__(self) -> str:
        return (
            f"Classifier(rules={self.rules_path!r}, "
            f"default={self.default_destination!r}, "
            f"rules={len(self._ext_map)})"
        )


# ---------------------------------------------------------------------- #
# Module-level default instance + functional API (for simple callers)
# ---------------------------------------------------------------------- #
_default_classifier: Optional[Classifier] = None


def _get_default() -> Classifier:
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = Classifier()
    return _default_classifier


def classify(event: FileEvent) -> Optional[ProposedAction]:
    """
    Functional wrapper around the default Classifier instance.
    Allows `from core.classifier import classify` without instantiating a class.
    """
    return _get_default().classify(event)
