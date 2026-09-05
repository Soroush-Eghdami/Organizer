"""Entry: GUI by default, CLI with --cli. Use --help for both."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CLI_SUBCOMMANDS = {"watch", "list", "review", "move", "clear"}


def _should_launch_gui(argv: list[str]) -> bool:
    if not argv:
        return True
    if "--gui" in argv:
        return True
    if "--cli" in argv:
        return False
    for tok in argv:
        if tok.startswith("-"):
            continue
        return tok not in CLI_SUBCOMMANDS
    return True


def _launch_gui(argv: list[str]) -> int:
    argv = [a for a in argv if a != "--gui"]
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
    app = OrganizerGUI(watch_path=args.watch, organize_root=args.organize, db_path=args.db or "queue.db", rules_path=args.rules)
    app.mainloop()
    return 0


def _launch_cli(argv: list[str]) -> int:
    argv = [a for a in argv if a != "--cli"]
    from cli.cli_app import main as cli_main
    return cli_main(argv)


def main() -> int:
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        print("\n--- CLI help ---\n")
        try:
            from cli.cli_app import build_parser
            build_parser().print_help()
        except Exception:
            pass
        print("\n--- GUI ---\n  python main.py              # GUI\n  python main.py --gui [--watch <folder> --organize <folder> --db <path>]")
        return 0
    if _should_launch_gui(argv):
        try:
            return _launch_gui(argv)
        except Exception as e:
            print(f"GUI launch failed ({e}), falling back to CLI. Use --cli.", file=sys.stderr)
            return _launch_cli(argv)
    return _launch_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
