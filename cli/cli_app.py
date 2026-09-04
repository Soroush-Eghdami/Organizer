from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path for `python cli/cli_app.py` direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import FileEvent, ActionStatus
from core.classifier import Classifier
from core.queue_manager import QueueManager
from core.watcher import FolderWatcher
from core.mover import Mover

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Logging setup — activity.log is owned here (main.py would also do it,
# but CLI should be runnable standalone). Keep mover simple, configure once here.
# ------------------------------------------------------------------ #
def setup_logging(log_path: str | Path | None = None) -> None:
    if log_path is None:
        log_path = Path(__file__).resolve().parent.parent / "logs" / "activity.log"
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_path), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _print_pending(qm: QueueManager, limit: int = 20) -> list:
    pending = qm.get_pending(limit=limit)
    if not pending:
        print("No pending items.")
        return pending
    print(f"\nPending queue ({qm.count_pending()} total, showing {len(pending)}):")
    print("-" * 80)
    for idx, a in enumerate(pending, 1):
        print(
            f"{idx:2}. [{a.id[:8]}] {a.filename or a.src_path} "
            f"-> {a.suggested_dest}  ({a.matched_rule})  [{a.extension}]"
        )
        print(f"     src: {a.src_path}")
    print("-" * 80)
    return pending


def scan_existing_files(
    watch_path: str | Path,
    qm: QueueManager,
    classifier: Classifier,
    recursive: bool = False,
) -> int:
    """
    Startup scan: queue files already sitting in watch_path that are not
    yet in the pending queue. Handles:
      * files that arrived while the app was closed
      * files that were still settling when SIGINT cancelled timers
    Returns count of newly queued files.
    """
    watch_path = Path(watch_path)
    if not watch_path.is_dir():
        return 0

    pattern = "**/*" if recursive else "*"
    queued = 0
    for p in watch_path.glob(pattern):
        if p.is_dir():
            continue
        # Skip the queue DB itself if it's inside the watch folder
        # and any hidden/temp files
        if p.name.startswith("."):
            continue
        src = str(p.resolve())
        # Dedup: skip if already pending (approved/moved already handled separately)
        if qm.has_pending_for_path(src):
            continue
        # Also skip if file already exists as MOVED? No — file was moved, not here.
        # For pending check we also consider exact path; if same file was rejected
        # we allow re-queue (user may have changed rules). So only skip pending.
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
    else:
        logger.debug("scan found no new files in %s", watch_path)
    return queued


# ------------------------------------------------------------------ #
# Core wiring: FolderWatcher -> Classifier -> Queue
# ------------------------------------------------------------------ #
def make_on_file_ready(classifier: Classifier, qm: QueueManager):
    """Bridge for FolderWatcher: FileEvent -> classify -> queue (dedup)."""

    def on_file_ready(event: FileEvent) -> None:
        try:
            # Classifier is pure, QueueManager handles dedup
            action = classifier.classify(event)
            if action is None:
                logger.debug("CLI: classifier skipped %s (is_dir=%s)", event.src_path, event.is_directory)
                return
            qm.add(action, dedup=True)
            # Fetch canonical row (dedup may have updated existing)
            stored = qm.find_pending_by_path(event.src_path) or action
            print(f"[queued] {stored.filename} -> {stored.suggested_dest} ({stored.matched_rule}) [{stored.id[:8]}]")
            logger.info("CLI queued %s -> %s [id=%s]", stored.src_path, stored.suggested_dest, stored.id)
        except Exception:
            logger.exception("CLI on_file_ready failed for %s", event.src_path)

    return on_file_ready


# ------------------------------------------------------------------ #
# Interactive review loop
# ------------------------------------------------------------------ #
def review_loop(qm: QueueManager, mover: Mover | None = None) -> None:
    """
    Manual test/interactive mode for the whole pipeline.
    Lists pending, prompts a/r per item, then optionally moves approved.
    """
    while True:
        pending = _print_pending(qm, limit=20)
        if not pending:
            # Offer to move approved even if no pending
            if qm.count(status=ActionStatus.APPROVED) > 0:
                ans = input("No pending left. Move approved now? [y/N/q]: ").strip().lower()
                if ans == "y" and mover:
                    summary = mover.move_all_approved()
                    print(f"Moved: {summary['moved']}, Failed: {summary['failed']}")
                elif ans == "q":
                    break
                else:
                    break
            else:
                break

        print("Commands: [a]pprove <num|all>  [r]eject <num|all>  [m]ove approved  [q]uit  [Enter=refresh]")
        raw = input("> ").strip().lower()
        if not raw:
            continue
        if raw in ("q", "quit", "exit"):
            break

        if raw in ("m", "move") and mover:
            summary = mover.move_all_approved()
            print(f"Moved: {summary['moved']}, Failed: {summary['failed']}")
            continue

        # parse "a 1", "a all", "r 2", etc. or just "a" to approve all
        parts = raw.split()
        cmd = parts[0]
        target = parts[1] if len(parts) > 1 else "all"

        if cmd in ("a", "approve", "y", "yes"):
            if target in ("all", "*"):
                for a in pending:
                    qm.update_status(a.id, ActionStatus.APPROVED)
                print(f"Approved {len(pending)} items.")
            else:
                try:
                    idx = int(target) - 1
                    sel = pending[idx]
                    qm.update_status(sel.id, ActionStatus.APPROVED)
                    print(f"Approved {sel.filename}")
                except (ValueError, IndexError):
                    # Try as id prefix
                    matched = [a for a in pending if a.id.startswith(target)]
                    if matched:
                        qm.update_status(matched[0].id, ActionStatus.APPROVED)
                        print(f"Approved {matched[0].filename}")
                    else:
                        print(f"Invalid target: {target}")
        elif cmd in ("r", "reject", "n", "no"):
            if target in ("all", "*"):
                for a in pending:
                    qm.update_status(a.id, ActionStatus.REJECTED)
                print(f"Rejected {len(pending)} items.")
            else:
                try:
                    idx = int(target) - 1
                    sel = pending[idx]
                    qm.update_status(sel.id, ActionStatus.REJECTED)
                    print(f"Rejected {sel.filename}")
                except (ValueError, IndexError):
                    matched = [a for a in pending if a.id.startswith(target)]
                    if matched:
                        qm.update_status(matched[0].id, ActionStatus.REJECTED)
                        print(f"Rejected {matched[0].filename}")
                    else:
                        print(f"Invalid target: {target}")
        else:
            print("Unknown command. Use a/r/m/q")


# ------------------------------------------------------------------ #
# Argparse quirk fix: normalize shared flags so they work before OR after subcommand
# ------------------------------------------------------------------ #
_SHARED_FLAGS = {
    "--watch": True,
    "--organize": True,
    "--db": True,
    "--rules": True,
    "--limit": True,
    "--recursive": False,  # boolean, no value
}
_KNOWN_SUBCOMMANDS = {"watch", "list", "review", "move", "clear"}


def _normalize_argv(argv: list[str] | None) -> list[str] | None:
    """
    Allow shared flags to appear either before or after the subcommand:

        cli_app.py --organize /path move  ==  cli_app.py move --organize /path

    Argparse by default only parses global flags before the subcommand.
    We rewrite argv so that any shared flag appearing after the subcommand
    is moved before it, preserving order (last wins).
    """
    if argv is None:
        return None
    # Find subcommand position
    sub_idx = None
    for i, tok in enumerate(argv):
        if tok in _KNOWN_SUBCOMMANDS:
            sub_idx = i
            break
    if sub_idx is None:
        return argv  # no subcommand, nothing to normalize

    before = argv[:sub_idx]
    after = argv[sub_idx + 1 :]
    # Extract shared flags from `after`
    moved: list[str] = []
    remaining_after: list[str] = []
    i = 0
    while i < len(after):
        tok = after[i]
        if tok in _SHARED_FLAGS:
            has_val = _SHARED_FLAGS[tok]
            moved.append(tok)
            if has_val:
                if i + 1 < len(after):
                    moved.append(after[i + 1])
                    i += 2
                else:
                    i += 1
            else:
                i += 1
        elif tok.startswith("-"):
            # Unknown flag after subcommand — leave it for argparse to error correctly
            remaining_after.append(tok)
            i += 1
            # If it looks like it has a value, keep that too? Let argparse handle.
            # We only move known shared flags.
        else:
            remaining_after.append(tok)
            i += 1

    if not moved:
        return argv
    # Rebuild: moved shared flags + before + subcommand + remaining
    # Keep original `before` shared flags as well — argparse last-wins, so after's moved flags appended after before's flags will correctly override.
    return before + moved + [argv[sub_idx]] + remaining_after


# ------------------------------------------------------------------ #
# CLI entry
# ------------------------------------------------------------------ #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Organizer CLI — watch -> classify -> queue -> mover (flags work before or after subcommand)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--watch", type=str, default=None, help="Folder to watch (FolderWatcher)")
    p.add_argument("--organize", type=str, default=None, help="Organize root (Mover destination base)")
    p.add_argument("--db", type=str, default="queue.db", help="SQLite queue path")
    p.add_argument("--rules", type=str, default=None, help="Path to rules.json")
    p.add_argument("--recursive", action="store_true", help="Watch recursively")
    p.add_argument("--limit", type=int, default=20, help="Pending list limit")

    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("watch", help="Start watcher and block (Ctrl+C to stop)")
    sub.add_parser("list", help="List pending queue")
    sub.add_parser("review", help="Interactive approve/reject loop")
    sub.add_parser("move", help="Move all APPROVED")
    sub.add_parser("clear", help="Clear entire queue (for testing)")
    return p


def main(argv: list[str] | None = None) -> int:
    # Normalize so shared flags work before OR after subcommand
    if argv is None:
        argv = _normalize_argv(sys.argv[1:])
    else:
        argv = _normalize_argv(list(argv))
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging()

    watch_path = args.watch
    organize_root = args.organize
    db_path = args.db
    rules_path = args.rules

    # Core wiring — always available even without watch path
    qm = QueueManager(db_path=db_path)
    classifier = Classifier(rules_path=rules_path, queue_manager=qm)
    mover = Mover(organize_root=organize_root or str(Path.cwd() / "organized"), queue_manager=qm) if organize_root or args.cmd in ("move", "review") else None
    if args.cmd in ("move", "review") and mover is None:
        mover = Mover(organize_root=str(Path.cwd() / "organized"), queue_manager=qm)

    def _start_watcher_with_scan(target_watch: str) -> FolderWatcher:
        """Helper: scan existing files, then start watcher. Prints scan summary."""
        scan_existing_files(target_watch, qm, classifier, recursive=args.recursive)
        on_ready = make_on_file_ready(classifier, qm)
        watcher = FolderWatcher(watch_path=target_watch, on_file_ready=on_ready, recursive=args.recursive)
        watcher.start()
        return watcher

    def _stop_watcher_with_warning(watcher: FolderWatcher) -> None:
        cancelled = watcher.stop()
        if cancelled:
            print(
                f"\n[warning] {cancelled} file(s) were still settling and were not queued — "
                f"they remain in {watcher.watch_path} and will be caught on next watch startup scan. "
                f"Rerun with --watch to re-scan."
            )

    # No subcommand: auto-detect — if watch provided, start watching + interactive review
    if args.cmd is None:
        if watch_path:
            watcher = _start_watcher_with_scan(watch_path)
            try:
                print(f"Watching {watch_path} (recursive={args.recursive}) -> queue {db_path}")
                print(f"Organize root: {mover.organize_root if mover else '(set --organize to enable moves)'}")
                print("Watcher running. Drop files into the watched folder. Ctrl+C to switch to review.")
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping watcher...")
                _stop_watcher_with_warning(watcher)
            if mover:
                review_loop(qm, mover)
            else:
                _print_pending(qm, limit=args.limit)
        else:
            _print_pending(qm, limit=args.limit)
            print("\nTip: use `review` to approve/reject, `move` to move approved, or `watch --watch <folder>` to monitor.")
            print("      Flags work before or after subcommand: `move --organize /path` == `--organize /path move`")
        return 0

    if args.cmd == "watch":
        if not watch_path:
            parser.error("--watch <folder> is required for `watch` command")
        watcher = _start_watcher_with_scan(watch_path)
        try:
            print(f"Watching {watch_path} (recursive={args.recursive})")
            print("Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            _stop_watcher_with_warning(watcher)
        return 0

    if args.cmd == "list":
        _print_pending(qm, limit=args.limit)
        return 0

    if args.cmd == "review":
        if mover is None:
            mover = Mover(organize_root=str(Path.cwd() / "organized"), queue_manager=qm)
        watcher = None
        if watch_path:
            watcher = _start_watcher_with_scan(watch_path)
            print(f"Watcher running in background on {watch_path} (Ctrl+C to stop review)")
        try:
            review_loop(qm, mover)
        finally:
            if watcher:
                _stop_watcher_with_warning(watcher)
        return 0

    if args.cmd == "move":
        if mover is None:
            org = getattr(args, "organize", None) or organize_root or str(Path.cwd() / "organized")
            mover = Mover(organize_root=org, queue_manager=qm)
        summary = mover.move_all_approved()
        print(f"Moved: {summary['moved']}, Failed: {summary['failed']}")
        return 0

    if args.cmd == "clear":
        n = qm.clear()
        print(f"Cleared {n} items.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
