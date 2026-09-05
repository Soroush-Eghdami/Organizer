from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import FileEvent, ActionStatus
from core.classifier import Classifier
from core.queue_manager import QueueManager
from core.watcher import FolderWatcher
from core.mover import Mover

try:
    from core.paths import get_app_dir
except ImportError:
    def get_app_dir():  # type: ignore
        return Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


def setup_logging(log_path: str | Path | None = None) -> None:
    """Configure file + stdout logging."""
    if log_path is None:
        log_path = get_app_dir() / "logs" / "activity.log"
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(str(log_path), encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _print_pending(qm: QueueManager, limit: int = 20) -> list:
    pending = qm.get_pending(limit=limit)
    if not pending:
        print("No pending items.")
        return pending
    print(f"\nPending queue ({qm.count_pending()} total, showing {len(pending)}):")
    print("-" * 80)
    for idx, a in enumerate(pending, 1):
        print(f"{idx:2}. [{a.id[:8]}] {a.filename or a.src_path} -> {a.suggested_dest} ({a.matched_rule}) [{a.extension}]")
        print(f"     src: {a.src_path}")
    print("-" * 80)
    return pending


def scan_existing_files(watch_path: str | Path, qm: QueueManager, classifier: Classifier, recursive: bool = False) -> int:
    """Queue files already in watch folder that aren't pending yet."""
    watch_path = Path(watch_path)
    if not watch_path.is_dir():
        return 0
    pattern = "**/*" if recursive else "*"
    queued = 0
    for p in watch_path.glob(pattern):
        if p.is_dir() or p.name.startswith("."):
            continue
        src = str(p.resolve())
        if qm.has_pending_for_path(src):
            continue
        try:
            ev = FileEvent.from_path(src)
            action = classifier.classify(ev)
            if action is None:
                continue
            qm.add(action, dedup=True)
            queued += 1
            logger.info("scan queued %s -> %s", src, action.suggested_dest)
        except Exception:
            logger.exception("scan failed for %s", p)
    if queued:
        print(f"[scan] queued {queued} existing file(s) from {watch_path} not yet in queue")
        logger.info("scan queued %d files from %s", queued, watch_path)
    return queued


def make_on_file_ready(classifier: Classifier, qm: QueueManager):
    """Watcher callback: classify -> queue."""
    def on_file_ready(event: FileEvent) -> None:
        try:
            action = classifier.classify(event)
            if action is None:
                return
            qm.add(action, dedup=True)
            stored = qm.find_pending_by_path(event.src_path) or action
            print(f"[queued] {stored.filename} -> {stored.suggested_dest} ({stored.matched_rule}) [{stored.id[:8]}]")
            logger.info("CLI queued %s -> %s [id=%s]", stored.src_path, stored.suggested_dest, stored.id)
        except Exception:
            logger.exception("CLI on_file_ready failed for %s", event.src_path)
    return on_file_ready


def review_loop(qm: QueueManager, mover: Mover | None = None) -> None:
    """Interactive approve/reject/move loop."""
    while True:
        pending = _print_pending(qm, limit=20)
        if not pending:
            if qm.count(status=ActionStatus.APPROVED) > 0:
                ans = input("No pending left. Move approved now? [y/N/q]: ").strip().lower()
                if ans == "y" and mover:
                    s = mover.move_all_approved()
                    print(f"Moved: {s['moved']}, Failed: {s['failed']}")
                elif ans == "q":
                    break
                else:
                    break
            else:
                break
        print("Commands: [a]pprove <num|all>  [r]eject <num|all>  [m]ove approved  [q]uit  [Enter=refresh]")
        raw = input("> ").strip().lower()
        if not raw or raw in ("q", "quit", "exit"):
            if not raw:
                continue
            break
        if raw in ("m", "move") and mover:
            s = mover.move_all_approved()
            print(f"Moved: {s['moved']}, Failed: {s['failed']}")
            continue
        parts = raw.split()
        cmd, target = parts[0], parts[1] if len(parts) > 1 else "all"
        if cmd in ("a", "approve", "y", "yes"):
            if target in ("all", "*"):
                for a in pending:
                    qm.update_status(a.id, ActionStatus.APPROVED)
                print(f"Approved {len(pending)} items.")
            else:
                try:
                    sel = pending[int(target) - 1]
                    qm.update_status(sel.id, ActionStatus.APPROVED)
                    print(f"Approved {sel.filename}")
                except (ValueError, IndexError):
                    m = [a for a in pending if a.id.startswith(target)]
                    print(f"Approved {m[0].filename}" if m and qm.update_status(m[0].id, ActionStatus.APPROVED) else f"Invalid target: {target}")
        elif cmd in ("r", "reject", "n", "no"):
            if target in ("all", "*"):
                for a in pending:
                    qm.update_status(a.id, ActionStatus.REJECTED)
                print(f"Rejected {len(pending)} items.")
            else:
                try:
                    sel = pending[int(target) - 1]
                    qm.update_status(sel.id, ActionStatus.REJECTED)
                    print(f"Rejected {sel.filename}")
                except (ValueError, IndexError):
                    m = [a for a in pending if a.id.startswith(target)]
                    print(f"Rejected {m[0].filename}" if m and qm.update_status(m[0].id, ActionStatus.REJECTED) else f"Invalid target: {target}")
        else:
            print("Unknown command. Use a/r/m/q")


# Allow flags before or after subcommand: move --organize == --organize move
_SHARED_FLAGS = {"--watch": True, "--organize": True, "--db": True, "--rules": True, "--limit": True, "--recursive": False}
_KNOWN_SUBCOMMANDS = {"watch", "list", "review", "move", "clear"}

def _normalize_argv(argv: list[str] | None) -> list[str] | None:
    if argv is None:
        return None
    sub_idx = next((i for i, t in enumerate(argv) if t in _KNOWN_SUBCOMMANDS), None)
    if sub_idx is None:
        return argv
    before, after = argv[:sub_idx], argv[sub_idx + 1 :]
    moved, remaining = [], []
    i = 0
    while i < len(after):
        tok = after[i]
        if tok in _SHARED_FLAGS:
            has_val = _SHARED_FLAGS[tok]
            moved.append(tok)
            if has_val and i + 1 < len(after):
                moved.append(after[i+1]); i += 2
            else:
                i += 1
        elif tok.startswith("-"):
            remaining.append(tok); i += 1
        else:
            remaining.append(tok); i += 1
    return before + moved + [argv[sub_idx]] + remaining if moved else argv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Organizer CLI — watch -> classify -> queue -> mover (flags work before or after subcommand)", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--watch", type=str, default=None, help="Folder to watch")
    p.add_argument("--organize", type=str, default=None, help="Organize root (Mover destination base)")
    p.add_argument("--db", type=str, default=str(get_app_dir() / "queue.db"), help="SQLite queue path")
    p.add_argument("--rules", type=str, default=None, help="Path to rules.json")
    p.add_argument("--recursive", action="store_true", help="Watch subfolders too")
    p.add_argument("--limit", type=int, default=20, help="Pending list limit")
    sub = p.add_subparsers(dest="cmd", required=False)
    sub.add_parser("watch", help="Start watcher and block (Ctrl+C to stop)")
    sub.add_parser("list", help="List pending queue")
    sub.add_parser("review", help="Interactive approve/reject loop")
    sub.add_parser("move", help="Move all APPROVED")
    sub.add_parser("clear", help="Clear entire queue (for testing)")
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = _normalize_argv(sys.argv[1:])
    else:
        argv = _normalize_argv(list(argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()
    watch_path, organize_root, db_path, rules_path = args.watch, args.organize, args.db, args.rules
    qm = QueueManager(db_path=db_path)
    classifier = Classifier(rules_path=rules_path, queue_manager=qm)
    mover = Mover(organize_root=organize_root or str(Path.cwd() / "organized"), queue_manager=qm) if organize_root or args.cmd in ("move", "review") else None
    if args.cmd in ("move", "review") and mover is None:
        mover = Mover(organize_root=str(Path.cwd() / "organized"), queue_manager=qm)

    def _start_watcher_with_scan(target: str) -> FolderWatcher:
        scan_existing_files(target, qm, classifier, recursive=args.recursive)
        on_ready = make_on_file_ready(classifier, qm)
        w = FolderWatcher(watch_path=target, on_file_ready=on_ready, recursive=args.recursive)
        w.start()
        return w

    def _stop_with_warning(w: FolderWatcher) -> None:
        c = w.stop()
        if c:
            print(f"\n[warning] {c} file(s) were still settling and were not queued — they remain in {w.watch_path} and will be caught on next watch startup scan. Rerun with --watch to re-scan.")

    if args.cmd is None:
        if watch_path:
            w = _start_watcher_with_scan(watch_path)
            try:
                print(f"Watching {watch_path} (recursive={args.recursive}) -> queue {db_path}")
                print(f"Organize root: {mover.organize_root if mover else '(set --organize to enable moves)'}")
                print("Watcher running. Drop files into the watched folder. Ctrl+C to switch to review.")
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping watcher...")
                _stop_with_warning(w)
            if mover:
                review_loop(qm, mover)
            else:
                _print_pending(qm, limit=args.limit)
        else:
            _print_pending(qm, limit=args.limit)
            print("\nTip: use `review` to approve/reject, `move` to move approved, or `watch --watch <folder>` to monitor.")
        return 0

    if args.cmd == "watch":
        if not watch_path:
            parser.error("--watch <folder> is required for `watch` command")
        w = _start_watcher_with_scan(watch_path)
        try:
            print(f"Watching {watch_path} (recursive={args.recursive})")
            print("Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            _stop_with_warning(w)
        return 0

    if args.cmd == "list":
        _print_pending(qm, limit=args.limit)
        return 0

    if args.cmd == "review":
        if mover is None:
            mover = Mover(organize_root=str(Path.cwd() / "organized"), queue_manager=qm)
        w = None
        if watch_path:
            w = _start_watcher_with_scan(watch_path)
            print(f"Watcher running in background on {watch_path} (Ctrl+C to stop review)")
        try:
            review_loop(qm, mover)
        finally:
            if w:
                _stop_with_warning(w)
        return 0

    if args.cmd == "move":
        if mover is None:
            org = getattr(args, "organize", None) or organize_root or str(Path.cwd() / "organized")
            mover = Mover(organize_root=org, queue_manager=qm)
        s = mover.move_all_approved()
        print(f"Moved: {s['moved']}, Failed: {s['failed']}")
        return 0

    if args.cmd == "clear":
        print(f"Cleared {qm.clear()} items.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
