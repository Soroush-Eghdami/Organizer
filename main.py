"""Main entry - launches GUI by default, CLI on demand.

Usage:
  python main.py                 # -> GUI (CustomTkinter)
  python main.py --gui           # -> GUI
  python main.py --cli list      # -> CLI
  python main.py --cli --help    # -> CLI help
  python main.py --help          # -> this help (both)

Direct launches also work:
  python cli/cli_app.py --help
  python gui/gui_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root on path when run as `python main.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

CLI_SUBCOMMANDS = {"watch", "list", "review", "move", "clear"}
CLI_FLAGS = {"--watch", "--organize", "--db", "--rules", "--recursive", "--limit", "-h", "--help"}


def _should_launch_gui(argv: list[str]) -> bool:
    if not argv:
        return True  # no args → GUI (easy double-click)
    if "--gui" in argv:
        return True
    if "--cli" in argv:
        return False
    # If first non-flag looks like a CLI subcommand, treat as CLI
    for tok in argv:
        if tok.startswith("-"):
            continue
        return tok not in CLI_SUBCOMMANDS  # if not a CLI subcommand, maybe GUI flag → GUI
    return True


def _launch_gui(argv: list[str]) -> int:
    # Strip --gui so GUI doesn't see it
    argv = [a for a in argv if a != "--gui"]
    # Allow passing --watch/--organize/--db through to GUI
    watch = organize = db = rules = None
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--watch", type=str, default=None)
    p.add_argument("--organize", type=str, default=None)
    p.add_argument("--db", type=str, default=None)
    p.add_argument("--rules", type=str, default=None)
    args, _ = p.parse_known_args(argv)

    try:
        from gui.gui_app import OrganizerGUI
    except ImportError as e:
        print(f"GUI requires customtkinter: pip install customtkinter\n{e}", file=sys.stderr)
        return 1

    app = OrganizerGUI(
        watch_path=args.watch,
        organize_root=args.organize,
        db_path=args.db or "queue.db",
        rules_path=args.rules,
    )
    app.mainloop()
    return 0


def _launch_cli(argv: list[str]) -> int:
    # Strip --cli marker if present
    argv = [a for a in argv if a != "--cli"]
    from cli.cli_app import main as cli_main
    return cli_main(argv)


def main() -> int:
    argv = sys.argv[1:]

    if "-h" in argv or "--help" in argv:
        # Show combined help
        print(__doc__)
        print("\n--- CLI help ---\n")
        try:
            from cli.cli_app import build_parser
            build_parser().print_help()
        except Exception:
            pass
        print("\n--- GUI ---\n  python main.py              # launch GUI\n  python main.py --gui [--watch <folder> --organize <folder> --db <path>]\n  python gui/gui_app.py")
        return 0

    if _should_launch_gui(argv):
        # Default to GUI; if GUI fails (no display), fall back to CLI help
        try:
            return _launch_gui(argv)
        except Exception as e:
            # Headless / missing Tk — fall back to CLI
            print(f"GUI launch failed ({e}), falling back to CLI. Use --cli for CLI mode.", file=sys.stderr)
            return _launch_cli(argv)

    return _launch_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
