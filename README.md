# Organizer — CLI (v1, GUI skipped for now)

Automated download-folder sorter. Watches a folder, classifies files by extension via `config/rules.json`, queues them for your approval, then moves the approved ones — never silently overwriting anything.

**Stack:** Python 3.13, `watchdog` (debounced watcher), `sqlite3` (`queue.db`), stdlib `logging` → `logs/activity.log`.

---

## Quick start

```powershell
# 1. create / activate venv + install
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install watchdog

# 2. check rules
cat config\rules.json

# 3. run CLI (from project root D:\Code-Base\VibeCode\organizer)
python cli\cli_app.py --help
```

All flags work **before or after** the subcommand:
```powershell
python cli\cli_app.py --organize D:\Organized move
python cli\cli_app.py move --organize D:\Organized   # same
```

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
* Edit file at runtime then restart CLI, or `Classifier.reload()` picks it up.

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
3. **Startup scan** — every `watch` does a manual `scan_existing_files(watch_path)` before `observer.start()`. This catches files that arrived while the app was closed **and** files whose debounce timer was cancelled on `Ctrl+C`.
4. **SIGINT gap** — `FolderWatcher.stop()` cancels in-flight timers and returns count. `_stop_watcher_with_warning()` prints: `[warning] N file(s) were still settling and were not queued — they remain in <watch> and will be caught on next watch startup scan.`

---

## Mover guarantees (`core/mover.py`)

* Only `APPROVED` moves — re-checks `status == APPROVED` even though caller should only pass approved (defense-in-depth).
* `organize_root / suggested_dest / filename` — `mkdir(parents=True, exist_ok=True)` if dest folder missing.
* **No overwrite:** `_resolve_name_collision()` → `report.pdf` → `report (1).pdf` → `report (2).pdf` … capped at 1000.
* `shutil.move` (cross-drive safe) wrapped in `try/except` — on any error `qm.set_error(id, msg)` and `return False`; never propagates to kill batch. Handles `source no longer exists`, `source is directory`, permission/disk-full/long-path.
* Success → `qm.update_status(MOVED)` + append to `logs/activity.log` via `logging` (configured once in CLI) with fallback direct append. Check `logs/activity.log` for `MOVED src -> dest [id=... rule=...]`.

---

## Logs & DB

* `logs/activity.log` — plain text, one line per move. Created automatically.
* `queue.db` — SQLite table `queue(id PK, src_path, suggested_dest, matched_rule, confidence, status, created_at, resolved_at, filename, extension, error_message)` + indexes on `status, created_at, src_path`. Safe to delete to reset. Use `--db :memory:` for tests. Swap to another `qc` path per watch folder if needed.

---

## Manual test checklist (what to try before GUI)

```powershell
# 1. clean
python cli\cli_app.py clear --db queue.db; Remove-Item logs\activity.log -ErrorAction SilentlyContinue

# 2. pre-existing files are caught by scan
echo "x" > D:\Downloads\old.jpg
python cli\cli_app.py --watch D:\Downloads --db queue.db watch   # Ctrl+C after 3s, check [scan] queued 1

# 3. debounce: drop 3 files quickly, correct count 3 not 6
python cli\cli_app.py --watch D:\Downloads list  # should show 3

# 4. fallback bucket
echo "x" > D:\Downloads\tool.exe
# wait 3s, list → tool.exe -> Unsorted (fallback)

# 5. duplicate: drop same file twice quickly → still 1 pending for that path
# 6. approve + move
python cli\cli_app.py --organize D:\Organized move
# check D:\Organized\Photos\old.jpg exists, no overwrite, second same name -> " (1).jpg"

# 7. SIGINT during settle: drop file, Ctrl+C within 1s → see [warning] ... still settling, rerun watch -> scan catches it

# 8. both flag orders:
python cli\cli_app.py move --organize D:\Organized --db queue.db
python cli\cli_app.py --organize D:\Organized --db queue.db move
```

---

## Project layout

```
organizer/
  cli/cli_app.py      # thin shell — wiring only, no business logic
  core/
    models.py         # FileEvent, ProposedAction, ActionStatus
    classifier.py     # pure extension → dest, Unsorted fallback
    queue_manager.py  # SQLite, dedup, thread-safe for watcher
    watcher.py        # debounced FolderWatcher
    mover.py          # approved-only moves, collision, logging
  config/rules.json
  logs/activity.log   # auto-created
  gui/gui_app.py      # skipped for v1 (CLI only)
  queue.db            # created on first run
```

GUI (`gui/gui_app.py`, CustomTkinter) intentionally left empty for now — CLI doubles as the interactive test harness. To add it later, reuse same wiring: `FolderWatcher(make_on_file_ready)` + `review_loop` → GUI table with Approve/Reject/Move buttons calling same `qm`/`mover` methods.

---

## Troubleshooting

* `watchdog is not installed` → `pip install watchdog` in venv.
* `watch_path does not exist` → create the folder first.
* No pending after drop → wait `debounce (2s) + settle (0.5s) ≈ 2.5s` before `Ctrl+C`; or just rerun `watch` to trigger scan.
* Moves go to `organizer/organized` instead of your path → you omitted `--organize`; pass it before or after subcommand.
