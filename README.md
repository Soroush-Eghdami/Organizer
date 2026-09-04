# Organizer — File Sorter (GUI + CLI)

Automated download-folder sorter. Watches a folder, classifies files by extension via `config/rules.json`, queues them for your approval, then moves the approved ones — never silently overwriting anything.

**Stack:** Python 3.13, `customtkinter` (GUI) / `watchdog` (debounced watcher), `sqlite3` (`queue.db`), stdlib `logging` → `logs/activity.log`.

---

## Quick start (GUI — default)

```powershell
# 1. venv + deps
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install watchdog customtkinter

# 2. check rules
cat config\rules.json

# 3. launch GUI (double-click friendly)
python main.py
# or
python gui\gui_app.py
# with args
python main.py --gui --watch D:\Downloads --organize D:\Organized
```

GUI is **powerful, fast, easy**: same `core/` as CLI, no business logic duplicated.

### Quick start (CLI)

```powershell
python main.py --cli --help
python cli\cli_app.py --help
python main.py --cli list --db queue.db
```

All CLI flags work **before or after** the subcommand (see CLI section).

---

## Launch matrix

| How you run | What happens |
|---|---|
| `python main.py` | **GUI** (no args → GUI) |
| `python main.py --gui` | GUI |
| `python main.py --gui --watch D:\Downloads --organize D:\Organized` | GUI pre-filled |
| `python gui\gui_app.py` | GUI direct |
| `python main.py --cli list` | CLI |
| `python main.py --cli --watch D:\Downloads watch` | CLI watch |
| `python cli\cli_app.py list` | CLI direct |
| `python main.py --help` | Combined help (GUI + CLI) |

`main.py` auto-detects: no args or `--gui` → GUI, otherwise CLI subcommand → CLI. If GUI fails (headless/no Tk), it falls back to CLI.

---

## GUI guide

![GUI layout: top controls, stats, bulk bar, tabs]

### Top bar
* **Watch:** path to monitor + **Browse** (filedialog). Example `D:\Downloads`.
* **Organize:** destination root + **Browse**. `Photos` rule → `<organize>\Photos\photo.jpg`.
* **Options:** `Recursive` checkbox (watch subfolders), `▶ Start Watching` / `■ Stop Watching`, `Scan Now`, `● watching/idle` dot.

### Stats row (auto-refresh 1s)
`Pending: N` **bold** | `Approved: N` green | `Moved: N` blue | `Rejected: N` red — from `QueueManager.count()`.

### Bulk bar
`Approve All Pending` (green) | `Reject All Pending` (red) | `▶ Move Approved` (blue, off-thread so UI stays responsive) | `↻ Refresh` | `Clear Rejected`.

### Tabs
* **Pending** — `CTkScrollableFrame`, each row: icon by ext (`🖼️ .jpg/.png`, `🎬 .mp4`, `📄 .pdf`, `🎵 .mp3`, `📦 fallback`), `filename → dest • rule • ext • id[:8]`, `src_path`, `⚠ error` if any, `✓ Approve` / `✕ Reject` per-row + `Open` (opens parent folder). Empty → hint.
* **Approved** — ready to move, same rows (no buttons). `Move Approved` moves all.
* **History** — `get_all(80)` filtered `MOVED/REJECTED` newest first.
* **Logs** — tail 80 lines of `logs/activity.log` (`CTkTextbox` disabled, auto `see(end)`).

### Flow (GUI)
1. Pick `Watch` + `Organize` → `▶ Start Watching`. GUI does **startup scan** first (catches files while app was closed + files cancelled mid-settle on last SIGINT, same as CLI).
2. Drop files into watch folder → after `debounce 2.0s + settle 0.5s ≈ 2.5s` they appear in **Pending** (toast `Queued X → Y`).
3. Per-file `✓ Approve` / `✕ Reject` or bulk `Approve All` → counts update.
4. `▶ Move Approved` → `Mover.move_all_approved()` off-thread → toast `Moved 4, Failed 0`, rows move to **History**, `logs/activity.log` appended.
5. `■ Stop Watching` — if `N` timers were still settling, GUI warns: `N file(s) were still settling and were not queued. They will be caught on next startup scan.` (also logged).
6. `Scan Now` — manual re-scan for missed files.
7. Close window → watcher stopped cleanly + polling cancelled.

**Performance:** `after(1000)` poll, `RLock` on `QueueManager` for `:memory:` shared connection (watcher thread vs GUI thread), `WAL` + indexes, rebuild only visible 100 rows.

---

## Configuration — `config/rules.json`

```json
{
  "default_destination": "Unsorted",
  "rules": [
    {"match": {"extension": [".jpg", ".jpeg", ".png"]}, "destination": "Photos"},
    {"match": {"extension": [".mp4", ".mkv"]}, "destination": "Videos"},
    {"match": {"extension": [".pdf", ".docx", ".txt"]}, "destination": "Documents"},
    {"match": {"extension": [".mp3", ".flac"]}, "destination": "Music"}
  ]
}
```

* `default_destination` — fallback bucket for `.exe/.zip/unknown` (hardcoded fallback is also `Unsorted` if key missing/corrupt). Files are **never silently ignored**.
* `rules[ ].match.extension` — case-insensitive, dot optional (`.jpg` == `jpg`). First rule wins.
* Edit at runtime then restart GUI/CLI, or `Classifier.reload()` picks it up.

---

## CLI usage

### Flags (shared, work before OR after subcommand)

```
--watch <folder>      Folder to watch (FolderWatcher)
--organize <folder>   Destination root (Mover). Suggested dest "Photos" -> "<organize>/Photos"
--db <path>           SQLite queue (default: queue.db, next to where you run)
--rules <path>        Custom rules.json
--recursive           Watch subfolders too
--limit <n>           List limit (default 20)
```

### Commands

```powershell
# List pending (nothing watched yet)
python cli\cli_app.py --db queue.db list
python cli\cli_app.py list --db queue.db   # same
python main.py --cli list --db queue.db

# Watch and queue (blocks, Ctrl+C to stop)
python cli\cli_app.py --watch D:\Downloads --organize D:\Organized watch
python cli\cli_app.py watch --watch D:\Downloads --organize D:\Organized --recursive

# Watch + interactive review (approves then moves)
python cli\cli_app.py --watch D:\Downloads --organize D:\Organized review

# Interactive review of already-queued items (no watcher)
python cli\cli_app.py --db queue.db review
python cli\cli_app.py review --db queue.db --organize D:\Organized

# Move all APPROVED (non-interactive)
python cli\cli_app.py --organize D:\Organized move
python cli\cli_app.py move --organize D:\Organized --db queue.db

# Clear queue (testing)
python cli\cli_app.py clear
python cli\cli_app.py clear --db queue.db

# No subcommand: auto-detects
python cli\cli_app.py --watch D:\Downloads              # watches then reviews
python cli\cli_app.py                                   # just lists pending
```

### Interactive review loop

```
Pending queue (3 total, showing 3):
 1. [a1b2c3d4] photo.jpg -> Photos (extension:.jpg -> Photos) [.jpg]
      src: D:\Downloads\photo.jpg
 ...
Commands: [a]pprove <num|all>  [r]eject <num|all>  [m]ove approved  [q]uit
> a all        # approve all
> r 2          # reject #2
> r a1b2       # reject by id prefix
> m            # move approved now
> q
```

`m` calls `Mover.move_all_approved()` → `{"moved":2,"failed":0}`. Only `APPROVED` moves; `PENDING`/`REJECTED` are refused (defense-in-depth).

---

## What happens under the hood

1. **Watcher** (`core/watcher.py`) — `FolderWatcher` wraps `watchdog.Observer` + `_DebouncedHandler`. Events fire on watcher thread, so dict of `Timer`s is `Lock`-protected. Debounce is restart-the-timer (default 2.0s) + settle check (0.5s size-stable poll) to avoid moving half-written large files. `on_created/on_modified/on_moved/on_closed` all funnel to `_schedule_settle_check`.
2. **Watcher → Queue** — on settle, bridge builds `FileEvent.from_path(path)` and calls `Classifier.classify(event)` (pure: `FileEvent` in, `ProposedAction` out, no DB). Fallback → `Unsorted`. Then `QueueManager.add(dedup=True)` — if a `PENDING` row for same `src_path` already exists (watcher double-fire), it **updates** the existing row instead of inserting a duplicate (fresh UUID would otherwise not dedupe via `INSERT OR REPLACE`). `count` stays 1.
3. **Startup scan** — every `watch` (CLI) and `Start Watching` (GUI) does a manual `scan_existing_files(watch_path)` before `observer.start()`. This catches files that arrived while the app was closed **and** files whose debounce timer was cancelled on `Ctrl+C`/window close.
4. **SIGINT gap** — `FolderWatcher.stop()` cancels in-flight timers and returns count. CLI `_stop_watcher_with_warning()` / GUI `stop_watching()` prints/shows: `[warning] N file(s) were still settling and were not queued — they remain in <watch> and will be caught on next watch startup scan.` Also logged via `logger.warning`.

---

## Mover guarantees (`core/mover.py`)

* Only `APPROVED` moves — re-checks `status == APPROVED` even though caller should only pass approved (defense-in-depth).
* `organize_root / suggested_dest / filename` — `mkdir(parents=True, exist_ok=True)` if dest folder missing.
* **No overwrite:** `_resolve_name_collision()` → `report.pdf` → `report (1).pdf` → `report (2).pdf` … capped at 1000.
* `shutil.move` (cross-drive safe) wrapped in `try/except` — on any error `qm.set_error(id, msg)` and `return False`; never propagates to kill batch. Handles `source no longer exists`, `source is directory`, permission/disk-full/long-path.
* Success → `qm.update_status(MOVED)` + append to `logs/activity.log` via `logging` (configured once in CLI/GUI) with fallback direct append. Check `logs/activity.log` for `MOVED src -> dest [id=... rule=...]`.

---

## Logs & DB

* `logs/activity.log` — plain text, one line per move. Created automatically. GUI tails it in **Logs** tab; CLI writes via `logging.FileHandler`.
* `queue.db` — SQLite table `queue(id PK, src_path, suggested_dest, matched_rule, confidence, status, created_at, resolved_at, filename, extension, error_message)` + indexes on `status, created_at, src_path`. Safe to delete to reset. Use `--db :memory:` for tests. Swap to another `qc` path per watch folder if needed.

---

## Manual test checklist

```powershell
# 1. clean
python cli\cli_app.py clear --db queue.db; Remove-Item logs\activity.log -ErrorAction SilentlyContinue

# GUI: launch, set Watch=D:\Downloads, Organize=D:\Organized, Start Watching

# 2. pre-existing files are caught by scan
echo "x" > D:\Downloads\old.jpg
# GUI: Stop/Start or Scan Now → Pending shows old.jpg -> Photos
# CLI: python cli\cli_app.py --watch D:\Downloads --db queue.db watch   # Ctrl+C after 3s, check [scan] queued 1

# 3. debounce: drop 3 files quickly, correct count 3 not 6
# GUI: drop 3 files, pending count should be 3 after 3s

# 4. fallback bucket
echo "x" > D:\Downloads\tool.exe
# GUI/CLI list → tool.exe -> Unsorted (fallback)

# 5. duplicate: drop same file twice quickly → still 1 pending for that path
# 6. approve + move
# GUI: Approve All → Move Approved → check D:\Organized\Photos\old.jpg exists, second same name -> " (1).jpg"
# CLI: python cli\cli_app.py --organize D:\Organized move

# 7. SIGINT during settle: drop file, Ctrl+C/close window within 1s → see [warning] ... still settling, rerun watch/Start Watching -> scan catches it

# 8. both flag orders (CLI):
python cli\cli_app.py move --organize D:\Organized --db queue.db
python cli\cli_app.py --organize D:\Organized --db queue.db move
```

---

## Project layout

```
organizer/
  main.py             # entry: GUI by default, --cli for CLI, --help for both
  cli/cli_app.py      # thin CLI shell — wiring only, no business logic
  gui/gui_app.py      # CustomTkinter GUI — same wiring, tabs + polling
  core/
    models.py         # FileEvent, ProposedAction, ActionStatus
    classifier.py     # pure extension → dest, Unsorted fallback
    queue_manager.py  # SQLite, dedup, thread-safe for watcher
    watcher.py        # debounced FolderWatcher
    mover.py          # approved-only moves, collision, logging
  config/rules.json
  logs/activity.log   # auto-created
  queue.db            # created on first run
  organized/          # created on first move (gitignored)
```

---

## Troubleshooting

* `watchdog is not installed` → `pip install watchdog` in venv.
* `customtkinter is not installed` → `pip install customtkinter` (GUI only).
* `watch_path does not exist` → create the folder first.
* No pending after drop → wait `debounce (2s) + settle (0.5s) ≈ 2.5s` before `Ctrl+C`/close; or click `Scan Now` / rerun `watch` to trigger scan.
* Moves go to `organizer/organized` instead of your path → you omitted `--organize`; pass it before or after subcommand (or set in GUI).
* GUI not opening (headless) → `main.py` falls back to CLI; run `python cli\cli_app.py --help`.
