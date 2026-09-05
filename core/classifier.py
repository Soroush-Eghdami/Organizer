from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Any

try:
    from core.models import FileEvent, ProposedAction
except ImportError:
    from .models import FileEvent, ProposedAction

try:
    from core.paths import get_app_dir
except ImportError:
    from .paths import get_app_dir

HARDCODED_FALLBACK = "Unsorted"

DEFAULT_RULES: list[dict] = [
    {"match": {"extension": [".jpg", ".jpeg", ".png"]}, "destination": "Photos"},
    {"match": {"extension": [".mp4", ".mkv"]}, "destination": "Videos"},
    {"match": {"extension": [".pdf", ".docx", ".txt"]}, "destination": "Documents"},
    {"match": {"extension": [".mp3", ".flac"]}, "destination": "Music"},
]


class Classifier:
    """Maps FileEvent -> ProposedAction using rules.json. Pure classify(), no DB."""

    def __init__(self, rules_path: str | os.PathLike[str] | None = None, default_destination: Optional[str] = None, queue_manager: Optional[Any] = None):
        if rules_path is None:
            rules_path = str(get_app_dir() / "config" / "rules.json")
        self.rules_path = str(rules_path)
        self._queue_manager = queue_manager
        self._explicit_fallback = default_destination
        self.default_destination: str = HARDCODED_FALLBACK
        self._ext_map: Dict[str, str] = {}
        self._raw_rules: list[dict] = []
        self._load_rules()

    def _write_default_rules_file(self) -> None:
        """Create default config on first run so fresh installs aren't empty."""
        import copy
        data = {"default_destination": self._explicit_fallback or HARDCODED_FALLBACK, "rules": copy.deepcopy(DEFAULT_RULES)}
        try:
            Path(self.rules_path).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(self.rules_path).with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.write("\n")
            tmp.replace(self.rules_path)
            self.default_destination = data["default_destination"]
            self._raw_rules = data["rules"]
            self._load_rules_from_memory()
        except Exception:
            self.default_destination = self._explicit_fallback or HARDCODED_FALLBACK
            self._raw_rules = copy.deepcopy(DEFAULT_RULES)
            self._load_rules_from_memory()

    def _load_rules(self) -> None:
        try:
            with open(self.rules_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._write_default_rules_file()
            return
        except json.JSONDecodeError:
            self.default_destination = self._explicit_fallback or HARDCODED_FALLBACK
            self._ext_map = {}
            self._raw_rules = []
            return

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
                if norm not in ext_map:
                    ext_map[norm] = str(dest)
        self._ext_map = ext_map

    def reload(self) -> None:
        self._load_rules()

    # Rule editing for GUI
    def get_rules(self) -> list[dict]:
        import copy
        return copy.deepcopy(self._raw_rules)

    def get_default_destination(self) -> str:
        return self.default_destination

    def set_default_destination(self, dest: str, autosave: bool = True) -> None:
        dest = dest.strip()
        if not dest:
            raise ValueError("default_destination cannot be empty")
        self.default_destination = dest
        self._explicit_fallback = dest
        if autosave:
            self.save()

    def _normalize_extensions(self, exts: list[str] | str) -> list[str]:
        if isinstance(exts, str):
            exts = [e.strip() for e in exts.replace(",", " ").split()]
        out, seen = [], set()
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
        """Rebuild ext map from current rules without reading file."""
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
        """Write current rules to disk atomically."""
        data = {"default_destination": self.default_destination, "rules": self._raw_rules}
        Path(self.rules_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(self.rules_path).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")
        tmp.replace(self.rules_path)
        self._load_rules()

    def classify(self, event: FileEvent) -> Optional[ProposedAction]:
        """Pure: FileEvent -> ProposedAction. Unmatched goes to default. No DB."""
        if not event or not event.src_path or event.is_directory:
            return None
        ext = (event.extension or Path(event.src_path).suffix).lower()
        if ext and not ext.startswith("."):
            ext = "." + ext
        dest = self._ext_map.get(ext)
        if dest:
            matched_rule = f"extension:{ext} -> {dest}"
            suggested_dest = dest
        else:
            suggested_dest = self.default_destination
            matched_rule = f"extension:{ext} -> {suggested_dest} (fallback)" if ext else f"no-extension -> {suggested_dest} (fallback)"
        return ProposedAction.from_file_event(event=event, suggested_dest=suggested_dest, matched_rule=matched_rule, confidence=1.0)

    def classify_and_queue(self, event: FileEvent, queue_manager: Optional[Any] = None) -> Optional[ProposedAction]:
        """Classify and queue with dedup. Only place that touches DB."""
        qm = queue_manager or self._queue_manager
        action = self.classify(event)
        if action is None or qm is None:
            return action
        qm.add(action, dedup=True)
        return qm.find_pending_by_path(action.src_path) or action

    @property
    def extension_map(self) -> Dict[str, str]:
        return dict(self._ext_map)

    def __repr__(self) -> str:
        return f"Classifier(rules={self.rules_path!r}, default={self.default_destination!r}, rules={len(self._ext_map)})"


_default_classifier: Optional[Classifier] = None

def _get_default() -> Classifier:
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = Classifier()
    return _default_classifier

def classify(event: FileEvent) -> Optional[ProposedAction]:
    return _get_default().classify(event)
