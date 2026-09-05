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
    # Rule editing (used by GUI)
    # ------------------------------------------------------------------ #
    def get_rules(self) -> list[dict]:
        """Return a deep copy of raw rules for display/editing."""
        import copy
        return copy.deepcopy(self._raw_rules)

    def get_default_destination(self) -> str:
        return self.default_destination

    def set_default_destination(self, dest: str, autosave: bool = True) -> None:
        dest = dest.strip()
        if not dest:
            raise ValueError("default_destination cannot be empty")
        self.default_destination = dest
        # Update explicit fallback so save persists it
        self._explicit_fallback = dest
        if autosave:
            self.save()

    def _normalize_extensions(self, exts: list[str] | str) -> list[str]:
        if isinstance(exts, str):
            # comma or space separated
            exts = [e.strip() for e in exts.replace(",", " ").split()]
        out = []
        seen = set()
        for e in exts:
            if not isinstance(e, str):
                continue
            e = e.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            if e not in seen:
                seen.add(e)
                out.append(e)
        return out

    def add_rule(self, extensions: list[str] | str, destination: str, autosave: bool = True) -> None:
        exts = self._normalize_extensions(extensions)
        dest = destination.strip()
        if not exts:
            raise ValueError("Provide at least one extension (e.g. .jpg, .pdf)")
        if not dest:
            raise ValueError("Destination cannot be empty")
        # Append as new rule
        self._raw_rules.append({"match": {"extension": exts}, "destination": dest})
        self._load_rules_from_memory()
        if autosave:
            self.save()

    def update_rule(self, index: int, extensions: list[str] | str, destination: str, autosave: bool = True) -> None:
        if not 0 <= index < len(self._raw_rules):
            raise IndexError("rule index out of range")
        exts = self._normalize_extensions(extensions)
        dest = destination.strip()
        if not exts:
            raise ValueError("Provide at least one extension")
        if not dest:
            raise ValueError("Destination cannot be empty")
        self._raw_rules[index] = {"match": {"extension": exts}, "destination": dest}
        self._load_rules_from_memory()
        if autosave:
            self.save()

    def delete_rule(self, index: int, autosave: bool = True) -> None:
        if not 0 <= index < len(self._raw_rules):
            raise IndexError("rule index out of range")
        del self._raw_rules[index]
        self._load_rules_from_memory()
        if autosave:
            self.save()

    def _load_rules_from_memory(self) -> None:
        """Rebuild _ext_map from current _raw_rules + default_destination without re-reading file."""
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
                if norm not in ext_map:
                    ext_map[norm] = str(dest)
        self._ext_map = ext_map

    def save(self) -> None:
        """Persist current rules + default_destination to rules_path."""
        data = {
            "default_destination": self.default_destination,
            "rules": self._raw_rules,
        }
        # Ensure parent exists
        Path(self.rules_path).parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file
        tmp = Path(self.rules_path).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")
        tmp.replace(self.rules_path)
        # Rebuild to ensure consistency
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
