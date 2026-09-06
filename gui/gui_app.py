from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
except ImportError as e:
    raise ImportError("customtkinter is required: pip install customtkinter") from e

from core.models import ActionStatus
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
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class OrganizerGUI(ctk.CTk):
    """Thin GUI over core. Fast polling, no logic duplication."""

    def __init__(self, watch_path: str | None = None, organize_root: str | None = None, db_path: str | None = None, rules_path: str | None = None, debounce: float = 2.0):
        super().__init__()
        self.title("Organizer — File Sorter")
        self.geometry("1100x720")
        self.minsize(900, 600)

        db_path = db_path or str(get_app_dir() / "queue.db")
        self.db_path, self.rules_path = db_path, rules_path
        self.qm = QueueManager(db_path=db_path)
        self.classifier = Classifier(rules_path=rules_path, queue_manager=self.qm)
        # Keep organize empty at first — show only placeholder like Watch
        self.organize_root = str(Path(organize_root).resolve()) if organize_root else ""
        self.mover = Mover(organize_root=self.organize_root or str(Path.home() / "Organized"), queue_manager=self.qm) if self.organize_root else None
        self.watcher: FolderWatcher | None = None
        self._debounce = debounce
        self._poll_after_id: str | None = None
        # User-tunable periods — default 2 s is smooth, not the old glitchy 1 s
        self._poll_interval_ms = 2000
        self._poll_var: ctk.StringVar | None = None
        self._debounce_var: ctk.StringVar | None = None
        self._last_poll_sig = None  # avoid rebuilding when nothing changed (flicker fix)
        self._last_log_mtime: float | None = None
        # Coalesced refresh — fixes freeze when 50+ files arrive at once (was 52× full rebuild)
        self._refresh_queued: bool = False
        self._refresh_after: str | None = None
        self._refreshing: bool = False
        self._toast_buffer: list[str] = []
        self._toast_after: str | None = None

        self._build_ui()
        # Sync option menus with initial values (after _build_ui creates vars)
        try:
            if self._poll_var is not None:
                self._poll_var.set("2.0s")
            if self._debounce_var is not None:
                self._debounce_var.set(f"{debounce:g}s" if debounce == int(debounce) else f"{debounce}s")
        except Exception:
            pass
        self._refresh_all()
        if watch_path and Path(watch_path).is_dir():
            self._watch_entry.delete(0, "end"); self._watch_entry.insert(0, watch_path)
            if self.organize_root:
                self._organize_entry.delete(0, "end"); self._organize_entry.insert(0, self.organize_root)
            self.start_watching()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # UI
    def _build_ui(self) -> None:
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(top, text="Watch:", width=60).grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self._watch_entry = ctk.CTkEntry(top, placeholder_text="D:\\Downloads", width=340)
        self._watch_entry.grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        ctk.CTkButton(top, text="Browse", width=80, command=self._browse_watch).grid(row=0, column=2, padx=6)
        ctk.CTkLabel(top, text="Organize:", width=60).grid(row=0, column=3, padx=6, pady=6, sticky="w")
        self._organize_entry = ctk.CTkEntry(top, placeholder_text="D:\\Organized", width=340)
        self._organize_entry.grid(row=0, column=4, padx=6, pady=6, sticky="ew")
        if self.organize_root:
            self._organize_entry.insert(0, self.organize_root)
        ctk.CTkButton(top, text="Browse", width=80, command=self._browse_organize).grid(row=0, column=5, padx=6)
        ctk.CTkLabel(top, text="Options:", width=60).grid(row=1, column=0, padx=6, pady=6, sticky="w")
        self._recursive_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(top, text="Recursive", variable=self._recursive_var).grid(row=1, column=1, padx=6, sticky="w")
        self._watch_btn = ctk.CTkButton(top, text="▶ Start Watching", width=140, fg_color="#1f6aa5", command=self.toggle_watching)
        self._watch_btn.grid(row=1, column=2, padx=6)
        self._scan_btn = ctk.CTkButton(top, text="Scan Now", width=100, fg_color="#555555", command=self.scan_now)
        self._scan_btn.grid(row=1, column=3, padx=6)
        self._status_dot = ctk.CTkLabel(top, text="● idle", text_color="#888888", width=100)
        self._status_dot.grid(row=1, column=4, padx=6)

        # Timing controls — user can tune how often GUI refreshes and how long watcher debounces
        # Row 2: Poll interval (GUI refresh) + Debounce (watcher settle) — fixes 1 s flicker
        ctk.CTkLabel(top, text="Refresh:", width=55).grid(row=2, column=0, padx=6, pady=(2, 6), sticky="w")
        self._poll_var = ctk.StringVar(value="2.0s")
        self._poll_menu = ctk.CTkOptionMenu(top, variable=self._poll_var, values=["0.5s", "1.0s", "1.5s", "2.0s", "3.0s", "5.0s"], width=90, command=self._on_poll_interval_change)
        self._poll_menu.grid(row=2, column=1, padx=6, pady=(2, 6), sticky="w")
        ctk.CTkLabel(top, text="Debounce:", width=70).grid(row=2, column=2, padx=6, pady=(2, 6), sticky="w")
        self._debounce_var = ctk.StringVar(value=f"{self._debounce:g}s" if self._debounce == int(self._debounce) else f"{self._debounce}s")
        self._debounce_menu = ctk.CTkOptionMenu(top, variable=self._debounce_var, values=["0.5s", "1.0s", "1.5s", "2.0s", "2.5s", "3.0s"], width=90, command=self._on_debounce_change)
        self._debounce_menu.grid(row=2, column=3, padx=6, pady=(2, 6), sticky="w")
        ctk.CTkLabel(top, text="(Refresh = GUI poll; Debounce = settle before queue)", text_color="#6b7280", font=("Segoe UI", 10)).grid(row=2, column=4, columnspan=2, padx=6, pady=(2, 6), sticky="w")

        stats = ctk.CTkFrame(self)
        stats.pack(fill="x", padx=12, pady=6)
        self._pending_label = ctk.CTkLabel(stats, text="Pending: 0", font=("Segoe UI", 13, "bold"))
        self._pending_label.pack(side="left", padx=12, pady=6)
        self._approved_label = ctk.CTkLabel(stats, text="Approved: 0", text_color="#4ade80")
        self._approved_label.pack(side="left", padx=12)
        self._moved_label = ctk.CTkLabel(stats, text="Moved: 0", text_color="#60a5fa")
        self._moved_label.pack(side="left", padx=12)
        self._rejected_label = ctk.CTkLabel(stats, text="Rejected: 0", text_color="#f87171")
        self._rejected_label.pack(side="left", padx=12)

        bulk = ctk.CTkFrame(self)
        bulk.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(bulk, text="Approve All Pending", width=160, fg_color="#16a34a", hover_color="#15803d", command=self.approve_all).pack(side="left", padx=6, pady=6)
        ctk.CTkButton(bulk, text="Reject All Pending", width=160, fg_color="#dc2626", hover_color="#b91c1c", command=self.reject_all).pack(side="left", padx=6)
        ctk.CTkButton(bulk, text="▶ Move Approved", width=160, fg_color="#2563eb", hover_color="#1d4ed8", command=self.move_approved).pack(side="left", padx=6)
        ctk.CTkButton(bulk, text="↻ Refresh", width=100, fg_color="#4b5563", command=self._refresh_all).pack(side="right", padx=6)
        ctk.CTkButton(bulk, text="Clear Rejected", width=120, fg_color="#6b7280", command=self.clear_rejected).pack(side="right", padx=6)

        self.tabs = ctk.CTkTabview(self, command=self._on_tab_change)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=6)
        self.tabs.add("Pending"); self.tabs.add("Approved"); self.tabs.add("History"); self.tabs.add("Rules"); self.tabs.add("Logs")
        self._pending_dirty = False; self._approved_dirty = False; self._history_dirty = False

        self.pending_scroll = ctk.CTkScrollableFrame(self.tabs.tab("Pending"), label_text="Awaiting review — Approve or Reject per file")
        self.pending_scroll.pack(fill="both", expand=True, padx=6, pady=6)
        self.approved_scroll = ctk.CTkScrollableFrame(self.tabs.tab("Approved"), label_text="Ready to move")
        self.approved_scroll.pack(fill="both", expand=True, padx=6, pady=6)
        self.history_scroll = ctk.CTkScrollableFrame(self.tabs.tab("History"), label_text="Moved / Rejected (newest first)")
        self.history_scroll.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_rules_tab()
        self.log_text = ctk.CTkTextbox(self.tabs.tab("Logs"), wrap="word", height=200)
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text.insert("1.0", "Activity log — tail of logs/activity.log\n\n")
        self.log_text.configure(state="disabled")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(footer, text="Tip: drop files into watch folder — appears in Pending after ~2.5s. Use Scan Now if needed.", text_color="#9ca3af", font=("Segoe UI", 11)).pack(side="left")
        self._footer_status = ctk.CTkLabel(footer, text="", text_color="#4ade80")
        self._footer_status.pack(side="right")
        top.grid_columnconfigure(1, weight=1); top.grid_columnconfigure(4, weight=1)

    def _build_rules_tab(self) -> None:
        tab = self.tabs.tab("Rules")
        ctk.CTkLabel(tab, text="Edit how files are classified. Changes save instantly to config/rules.json", text_color="#9ca3af", font=("Segoe UI", 11)).pack(anchor="w", padx=8, pady=(8, 4))
        f = ctk.CTkFrame(tab)
        f.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(f, text="Default folder (Unsorted):", width=180).pack(side="left", padx=6, pady=8)
        self._default_dest_entry = ctk.CTkEntry(f, placeholder_text="Unsorted", width=200)
        self._default_dest_entry.pack(side="left", padx=6, pady=8)
        try:
            self._default_dest_entry.insert(0, self.classifier.get_default_destination())
        except Exception:
            pass
        ctk.CTkButton(f, text="Save Default", width=110, fg_color="#2563eb", command=self._save_default_dest).pack(side="left", padx=6, pady=8)
        ctk.CTkButton(f, text="↻ Reload", width=80, fg_color="#4b5563", command=self._reload_rules).pack(side="left", padx=6, pady=8)
        ctk.CTkLabel(f, text="for .exe/.zip/unknown", text_color="#6b7280", font=("Segoe UI", 10)).pack(side="left", padx=6)
        add = ctk.CTkFrame(tab)
        add.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(add, text="Add rule:", font=("Segoe UI", 12, "bold")).pack(side="left", padx=8, pady=8)
        self._new_ext_entry = ctk.CTkEntry(add, placeholder_text="Extensions: .jpg, .jpeg, .png", width=280)
        self._new_ext_entry.pack(side="left", padx=6, pady=8)
        self._new_dest_entry = ctk.CTkEntry(add, placeholder_text="Destination: Photos", width=180)
        self._new_dest_entry.pack(side="left", padx=6, pady=8)
        ctk.CTkButton(add, text="+ Add", width=80, fg_color="#16a34a", hover_color="#15803d", command=self._add_new_rule).pack(side="left", padx=6, pady=8)
        self.rules_scroll = ctk.CTkScrollableFrame(tab, label_text="Current rules — Edit or Delete")
        self.rules_scroll.pack(fill="both", expand=True, padx=8, pady=6)

    def _refresh_rules(self) -> None:
        try:
            cur = self.classifier.get_default_destination()
            if self._default_dest_entry.get().strip() != cur:
                self._default_dest_entry.delete(0, "end"); self._default_dest_entry.insert(0, cur)
        except Exception:
            pass
        self._clear_scroll(self.rules_scroll)
        try:
            rules = self.classifier.get_rules()
        except Exception as e:
            ctk.CTkLabel(self.rules_scroll, text=f"Failed: {e}", text_color="#f87171").pack(pady=20)
            return
        if not rules:
            ctk.CTkLabel(self.rules_scroll, text="No rules yet. Add one above.", text_color="#9ca3af").pack(pady=20)
            return
        for idx, rule in enumerate(rules):
            exts = rule.get("match", {}).get("extension", [])
            if isinstance(exts, str):
                exts = [exts]
            dest = rule.get("destination", "")
            row = ctk.CTkFrame(self.rules_scroll)
            row.pack(fill="x", padx=6, pady=4)
            ctk.CTkLabel(row, text=f"{idx+1}.", width=30, font=("Segoe UI", 11, "bold")).pack(side="left", padx=6, pady=8)
            ctk.CTkLabel(row, text=", ".join(exts), font=("Segoe UI", 11), text_color="#e5e7eb", width=300, anchor="w").pack(side="left", padx=6, pady=8)
            ctk.CTkLabel(row, text="→", text_color="#9ca3af", width=20).pack(side="left", padx=2)
            ctk.CTkLabel(row, text=dest, font=("Segoe UI", 11, "bold"), text_color="#60a5fa", width=160, anchor="w").pack(side="left", padx=6, pady=8)
            ctk.CTkLabel(row, text="").pack(side="left", expand=True)
            ctk.CTkButton(row, text="Edit", width=60, height=26, fg_color="#4b5563", hover_color="#374151", command=lambda i=idx: self._edit_rule(i)).pack(side="right", padx=4, pady=4)
            ctk.CTkButton(row, text="Delete", width=70, height=26, fg_color="#dc2626", hover_color="#b91c1c", command=lambda i=idx: self._delete_rule(i)).pack(side="right", padx=4, pady=4)

    def _save_default_dest(self) -> None:
        v = self._default_dest_entry.get().strip()
        if not v:
            messagebox.showwarning("Rules", "Default cannot be empty.")
            return
        try:
            self.classifier.set_default_destination(v, autosave=True)
            self._toast(f"Default → {v}"); self._refresh_rules(); self._refresh_all()
        except Exception as e:
            messagebox.showerror("Rules", str(e))

    def _reload_rules(self) -> None:
        try:
            self.classifier.reload()
            self._toast("Reloaded from config/rules.json"); self._refresh_rules(); self._refresh_all()
        except Exception as e:
            messagebox.showerror("Reload", str(e))

    def _add_new_rule(self) -> None:
        exts, dest = self._new_ext_entry.get().strip(), self._new_dest_entry.get().strip()
        if not exts or not dest:
            messagebox.showwarning("Rules", "Fill both extensions and destination.\nExample: .psd, .ai → Designs")
            return
        try:
            self.classifier.add_rule(exts, dest, autosave=True)
            self._new_ext_entry.delete(0, "end"); self._new_dest_entry.delete(0, "end")
            self._toast(f"Added {dest}"); self._refresh_rules(); self._refresh_all()
        except Exception as e:
            messagebox.showerror("Add rule", str(e))

    def _delete_rule(self, idx: int) -> None:
        try:
            rule = self.classifier.get_rules()[idx]
            exts, dest = ", ".join(rule.get("match", {}).get("extension", [])), rule.get("destination", "")
            if not messagebox.askyesno("Delete rule", f"Delete rule?\n\n{exts} → {dest}"):
                return
            self.classifier.delete_rule(idx, autosave=True)
            self._toast(f"Deleted {dest}"); self._refresh_rules(); self._refresh_all()
        except Exception as e:
            messagebox.showerror("Delete", str(e))

    def _edit_rule(self, idx: int) -> None:
        try:
            rule = self.classifier.get_rules()[idx]
            exts, dest = ", ".join(rule.get("match", {}).get("extension", [])), rule.get("destination", "")
        except Exception as e:
            messagebox.showerror("Edit", str(e)); return
        dlg = ctk.CTkToplevel(self)
        dlg.title(f"Edit rule #{idx+1}"); dlg.geometry("520x200"); dlg.transient(self); dlg.grab_set()
        ctk.CTkLabel(dlg, text="Extensions (comma separated):", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(16, 4))
        e1 = ctk.CTkEntry(dlg, width=480); e1.pack(padx=16, pady=4, fill="x"); e1.insert(0, exts)
        ctk.CTkLabel(dlg, text="Destination folder:", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(8, 4))
        e2 = ctk.CTkEntry(dlg, width=480); e2.pack(padx=16, pady=4, fill="x"); e2.insert(0, dest)
        def save():
            ne, nd = e1.get().strip(), e2.get().strip()
            if not ne or not nd:
                messagebox.showwarning("Edit", "Both fields required.", parent=dlg); return
            try:
                self.classifier.update_rule(idx, ne, nd, autosave=True)
                dlg.destroy(); self._toast(f"Updated → {nd}"); self._refresh_rules(); self._refresh_all()
            except Exception as e:
                messagebox.showerror("Edit", str(e), parent=dlg)
        f = ctk.CTkFrame(dlg, fg_color="transparent")
        f.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(f, text="Cancel", width=100, fg_color="#4b5563", command=dlg.destroy).pack(side="right", padx=6)
        ctk.CTkButton(f, text="Save", width=100, fg_color="#2563eb", command=save).pack(side="right", padx=6)

    # Watch
    def _browse_watch(self) -> None:
        d = filedialog.askdirectory(title="Choose folder to watch")
        if d:
            self._watch_entry.delete(0, "end"); self._watch_entry.insert(0, d)

    def _browse_organize(self) -> None:
        d = filedialog.askdirectory(title="Choose organize destination root")
        if d:
            self._organize_entry.delete(0, "end"); self._organize_entry.insert(0, d)
            self.organize_root = d
            self.mover = Mover(organize_root=d, queue_manager=self.qm)

    def _scan_now(self, watch_path: str) -> int:
        from cli.cli_app import scan_existing_files
        try:
            return scan_existing_files(watch_path, self.qm, self.classifier, recursive=self._recursive_var.get())
        except Exception:
            logger.exception("GUI scan failed")
            return 0

    def scan_now(self) -> None:
        wp = self._watch_entry.get().strip()
        if not wp or not Path(wp).is_dir():
            messagebox.showwarning("Scan", "Watch folder does not exist.")
            return
        n = self._scan_now(wp)
        self._toast(f"Scanned {n} new file(s)" if n else "Scan: nothing new")
        self._refresh_all()

    def _request_refresh(self, delay: int = 120) -> None:
        """Coalesce bursts (e.g. 52 files at once) into one UI rebuild."""
        if self._refresh_queued:
            return
        self._refresh_queued = True
        # Cancel previous pending if any (keep earliest)
        if self._refresh_after is not None:
            try:
                self.after_cancel(self._refresh_after)
            except Exception:
                pass
        self._refresh_after = self.after(delay, self._do_queued_refresh)

    def _do_queued_refresh(self) -> None:
        self._refresh_queued = False
        self._refresh_after = None
        # Flush buffered toasts as one line
        if self._toast_buffer:
            buf = self._toast_buffer[:]
            self._toast_buffer.clear()
            if len(buf) == 1:
                self._toast(buf[0])
            else:
                # Show batch summary, keep last dest as hint
                self._toast(f"Queued {len(buf)} files — e.g. {buf[-1]}")
        # Invalidate poll sig so next _poll will see fresh state even if we just refreshed
        self._last_poll_sig = None
        self._last_log_mtime = None
        try:
            self._refresh_all()
        except Exception:
            logger.exception("queued refresh failed")

    def _queue_toast(self, msg: str) -> None:
        """Buffer toasts from watcher threads and flush coalesced."""
        self._toast_buffer.append(msg)
        # Debounce flush — handled in _do_queued_refresh, but also fallback timer
        if self._toast_after is None:
            try:
                self._toast_after = self.after(300, self._flush_toasts)
            except Exception:
                pass

    def _flush_toasts(self) -> None:
        self._toast_after = None
        if not self._toast_buffer:
            return
        # If a refresh is already queued it will flush, otherwise flush now
        if self._refresh_queued:
            return
        buf = self._toast_buffer[:]
        self._toast_buffer.clear()
        if len(buf) == 1:
            self._toast(buf[0])
        else:
            self._toast(f"Queued {len(buf)} files")

    def _make_on_file_ready(self):
        def on_file_ready(event):
            try:
                action = self.classifier.classify(event)
                if action is None:
                    return
                self.qm.add(action, dedup=True)
                # Don't hammer main thread with 52× _refresh_all — coalesce
                self.after(0, lambda: self._request_refresh(150))
                self.after(0, lambda: self._queue_toast(f"{action.filename} → {action.suggested_dest}"))
            except Exception:
                logger.exception("GUI on_file_ready failed for %s", getattr(event, "src_path", "?"))
        return on_file_ready

    def start_watching(self) -> None:
        wp = self._watch_entry.get().strip()
        if not wp or not Path(wp).is_dir():
            messagebox.showwarning("Watch", "Please select a watch folder." if not wp else f"Folder not found:\n{wp}")
            return
        org = self._organize_entry.get().strip()
        if not org:
            messagebox.showwarning("Organize", "Please choose an Organize folder where sorted files will go.")
            return
        self.organize_root = org
        self.mover = Mover(organize_root=org, queue_manager=self.qm)
        self._scan_now(wp)
        if self.watcher and self.watcher.is_running():
            messagebox.showinfo("Watch", "Already watching.")
            return
        try:
            self.watcher = FolderWatcher(watch_path=wp, on_file_ready=self._make_on_file_ready(), recursive=self._recursive_var.get(), debounce_seconds=self._debounce)
            self.watcher.start()
            self._watch_btn.configure(text="■ Stop Watching", fg_color="#dc2626", hover_color="#b91c1c", command=self.stop_watching)
            self._status_dot.configure(text="● watching", text_color="#4ade80")
            self._toast(f"Watching {wp}"); logger.info("GUI watching %s", wp)
            self._start_polling()
        except Exception as e:
            messagebox.showerror("Watch", str(e))
            logger.exception("GUI start_watching failed")

    def stop_watching(self) -> None:
        if not self.watcher:
            return
        try:
            c = self.watcher.stop()
            if c:
                self._toast(f"{c} still settling — rescan next start")
                messagebox.showwarning("Watch stopped", f"{c} file(s) were still settling and were not queued.\nThey will be caught on next startup scan.")
        except Exception:
            logger.exception("GUI stop failed")
        finally:
            self.watcher = None
            self._watch_btn.configure(text="▶ Start Watching", fg_color="#1f6aa5", hover_color="#2563eb", command=self.start_watching)
            self._status_dot.configure(text="● idle", text_color="#888888")
            self._stop_polling()

    def toggle_watching(self) -> None:
        self.stop_watching() if self.watcher and self.watcher.is_running() else self.start_watching()

    # Queue
    def approve_all(self) -> None:
        pend = self.qm.get_pending(limit=1000)
        if not pend:
            self._toast("Nothing pending to approve"); return
        for a in pend:
            self.qm.update_status(a.id, ActionStatus.APPROVED)
        self._toast(f"Approved {len(pend)}"); self._refresh_all()

    def reject_all(self) -> None:
        pend = self.qm.get_pending(limit=1000)
        if not pend:
            self._toast("Nothing pending to reject"); return
        for a in pend:
            self.qm.update_status(a.id, ActionStatus.REJECTED)
        self._toast(f"Rejected {len(pend)}"); self._refresh_all()

    def clear_rejected(self) -> None:
        n = self.qm.clear(status=ActionStatus.REJECTED)
        self._toast(f"Cleared {n} rejected"); self._refresh_all()

    def move_approved(self) -> None:
        org = self._organize_entry.get().strip()
        if not org:
            messagebox.showwarning("Organize", "Please choose an Organize folder first.")
            return
        if self.mover is None or self.organize_root != org:
            self.organize_root = org
            self.mover = Mover(organize_root=org, queue_manager=self.qm)
        if self.qm.count(status=ActionStatus.APPROVED) == 0:
            self._toast("No approved items to move"); return
        self._toast(f"Moving {self.qm.count(status=ActionStatus.APPROVED)} file(s)...")
        threading.Thread(target=self._move_thread, daemon=True).start()

    def _move_thread(self) -> None:
        try:
            s = self.mover.move_all_approved()
            self.after(0, lambda: self._on_move_done(s))
        except Exception as e:
            logger.exception("GUI move failed")
            self.after(0, lambda: messagebox.showerror("Move", str(e)))

    def _on_move_done(self, summary: dict) -> None:
        msg = f"Moved {summary.get('moved',0)}, Failed {summary.get('failed',0)}"
        if summary.get("failed"):
            messagebox.showwarning("Move", msg + "\nCheck logs/activity.log")
        else:
            self._toast(msg)
        self._refresh_all()

    def _approve_one(self, aid: str) -> None:
        self.qm.update_status(aid, ActionStatus.APPROVED); self._refresh_all()

    def _reject_one(self, aid: str) -> None:
        self.qm.update_status(aid, ActionStatus.REJECTED); self._refresh_all()

    # Rendering
    def _clear_scroll(self, scroll: ctk.CTkScrollableFrame) -> None:
        for w in scroll.winfo_children():
            w.destroy()

    def _add_row(self, parent: ctk.CTkScrollableFrame, action, show_approve: bool = False, show_reject: bool = False) -> None:
        row = ctk.CTkFrame(parent)
        row.pack(fill="x", padx=6, pady=4)
        icon_map = {".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".mp4": "🎬", ".mkv": "🎬", ".pdf": "📄", ".docx": "📄", ".txt": "📄", ".mp3": "🎵", ".flac": "🎵"}
        icon = icon_map.get((action.extension or "").lower(), "📦" if "fallback" in (action.matched_rule or "") else "📄")
        ctk.CTkLabel(row, text=f"{icon}  {action.filename or action.src_path}", font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(row, text=f"→ {action.suggested_dest}  •  {action.matched_rule}  •  {action.extension or '-'}  •  {action.id[:8]}", text_color="#9ca3af", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=8)
        ctk.CTkLabel(row, text=action.src_path, text_color="#6b7280", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=8, pady=(0, 4))
        if action.error_message:
            ctk.CTkLabel(row, text=f"⚠ {action.error_message}", text_color="#f87171", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=8, pady=(0, 4))
        if show_approve or show_reject:
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.pack(fill="x", padx=8, pady=(0, 6))
            if show_approve:
                ctk.CTkButton(btns, text="✓ Approve", width=90, height=26, fg_color="#16a34a", hover_color="#15803d", command=lambda aid=action.id: self._approve_one(aid)).pack(side="left", padx=4)
            if show_reject:
                ctk.CTkButton(btns, text="✕ Reject", width=90, height=26, fg_color="#dc2626", hover_color="#b91c1c", command=lambda aid=action.id: self._reject_one(aid)).pack(side="left", padx=4)
            ctk.CTkButton(btns, text="Open", width=70, height=26, fg_color="#4b5563", command=lambda p=action.src_path: self._open_path(p)).pack(side="right", padx=4)

    def _open_path(self, path: str) -> None:
        import subprocess
        p = Path(path)
        folder = str(p.parent) if p.exists() else str(Path(self._watch_entry.get()).resolve())
        try:
            if sys.platform == "win32":
                import os as _os
                _os.startfile(folder)  # type: ignore
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            logger.exception("open folder failed")

    def _on_tab_change(self) -> None:
        """Lazy rebuild — only render visible tab immediately."""
        try:
            cur = self.tabs.get()  # type: ignore
        except Exception:
            cur = None
        if cur == "Pending" and self._pending_dirty:
            self._pending_dirty = False; self._refresh_pending()
        elif cur == "Approved" and self._approved_dirty:
            self._approved_dirty = False; self._refresh_approved()
        elif cur == "History" and self._history_dirty:
            self._history_dirty = False; self._refresh_history()

    def _refresh_pending(self) -> None:
        try:
            self._clear_scroll(self.pending_scroll)
            pend = self.qm.get_pending(limit=100)
            if not pend:
                ctk.CTkLabel(self.pending_scroll, text="No pending — drop files into watch folder or click Scan Now.", text_color="#9ca3af").pack(pady=20)
            else:
                # Cap visible rows to avoid 50+ widget freeze; rest summarized
                cap = 60
                for a in pend[:cap]:
                    self._add_row(self.pending_scroll, a, show_approve=True, show_reject=True)
                if len(pend) > cap:
                    ctk.CTkLabel(self.pending_scroll, text=f"… and {len(pend)-cap} more (use Approve All or filter)", text_color="#9ca3af").pack(pady=8)
        except Exception:
            logger.exception("refresh pending failed")

    def _refresh_approved(self) -> None:
        try:
            self._clear_scroll(self.approved_scroll)
            apr = self.qm.list_by_status(ActionStatus.APPROVED)
            if not apr:
                ctk.CTkLabel(self.approved_scroll, text="No approved — approve from Pending then Move.", text_color="#9ca3af").pack(pady=20)
            else:
                for a in apr[:60]:
                    self._add_row(self.approved_scroll, a)
                if len(apr) > 60:
                    ctk.CTkLabel(self.approved_scroll, text=f"… and {len(apr)-60} more", text_color="#9ca3af").pack(pady=8)
        except Exception:
            logger.exception("refresh approved failed")

    def _refresh_history(self) -> None:
        try:
            self._clear_scroll(self.history_scroll)
            hist = [x for x in self.qm.get_all(limit=80) if x.status in (ActionStatus.MOVED, ActionStatus.REJECTED)]
            if not hist:
                ctk.CTkLabel(self.history_scroll, text="No history yet.", text_color="#9ca3af").pack(pady=20)
            else:
                for a in hist:
                    self._add_row(self.history_scroll, a)
        except Exception:
            logger.exception("refresh history failed")

    def _refresh_all(self) -> None:
        if self._refreshing:
            self._request_refresh(150)
            return
        self._refreshing = True
        try:
            try:
                self._pending_label.configure(text=f"Pending: {self.qm.count_pending()}")
                self._approved_label.configure(text=f"Approved: {self.qm.count(status=ActionStatus.APPROVED)}")
                self._moved_label.configure(text=f"Moved: {self.qm.count(status=ActionStatus.MOVED)}")
                self._rejected_label.configure(text=f"Rejected: {self.qm.count(status=ActionStatus.REJECTED)}")
            except Exception:
                pass
            # Lazy — only rebuild active tab now, defer others (cuts 52-file freeze from ~0.9s → ~0.3s)
            try:
                cur = self.tabs.get()  # type: ignore
            except Exception:
                cur = "Pending"
            if cur == "Pending":
                self._refresh_pending()
                self._approved_dirty = True; self._history_dirty = True
            elif cur == "Approved":
                self._refresh_approved()
                self._pending_dirty = True; self._history_dirty = True
            elif cur == "History":
                self._refresh_history()
                self._pending_dirty = True; self._approved_dirty = True
            else:
                # Rules/Logs active — defer all lists
                self._pending_dirty = True; self._approved_dirty = True; self._history_dirty = True
                # Still keep counts fresh; lists will build on tab switch
            try:
                if hasattr(self, "rules_scroll"):
                    self._refresh_rules()
            except Exception:
                logger.debug("refresh rules failed", exc_info=True)
            try:
                log_path = get_app_dir() / "logs" / "activity.log"
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
                    self.log_text.configure(state="normal")
                    self.log_text.delete("1.0", "end")
                    self.log_text.insert("1.0", "\n".join(lines) or "(empty)")
                    self.log_text.configure(state="disabled")
                    self.log_text.see("end")
            except Exception:
                pass
        finally:
            self._refreshing = False

    def _on_poll_interval_change(self, value: str) -> None:
        """User picked new refresh period — apply immediately."""
        try:
            secs = float(value.strip().lower().replace("s", ""))
            self._poll_interval_ms = max(500, int(secs * 1000))
            self._toast(f"Refresh every {secs:g}s")
            # Restart polling so new period takes effect right away
            if self.watcher and self.watcher.is_running():
                self._start_polling()
        except Exception:
            logger.debug("bad poll value %r", value, exc_info=True)

    def _on_debounce_change(self, value: str) -> None:
        """User picked new debounce — affects next start and live watcher."""
        try:
            secs = float(value.strip().lower().replace("s", ""))
            secs = max(0.5, min(5.0, secs))
            self._debounce = secs
            if self.watcher is not None:
                try:
                    self.watcher._handler.debounce_seconds = secs  # type: ignore
                except Exception:
                    pass
            self._toast(f"Debounce {secs:g}s")
        except Exception:
            logger.debug("bad debounce value %r", value, exc_info=True)

    def _get_poll_sig(self):
        """Light signature to skip full rebuild when nothing changed (flicker fix)."""
        try:
            # Counts are cheap; IDs catch same-count swaps
            pending = self.qm.get_pending(limit=50)
            approved = self.qm.list_by_status(ActionStatus.APPROVED)[:30]
            return (
                self.qm.count_pending(),
                self.qm.count(status=ActionStatus.APPROVED),
                self.qm.count(status=ActionStatus.MOVED),
                self.qm.count(status=ActionStatus.REJECTED),
                tuple(a.id for a in pending),
                tuple(a.id for a in approved),
            )
        except Exception:
            return None

    def _start_polling(self) -> None:
        self._stop_polling()
        self._poll()

    def _poll(self) -> None:
        try:
            # If a burst refresh is already queued, let it handle the rebuild
            if self._refresh_queued or self._refreshing:
                pass
            else:
                sig = self._get_poll_sig()
                try:
                    log_path = get_app_dir() / "logs" / "activity.log"
                    mtime = log_path.stat().st_mtime if log_path.exists() else None
                except Exception:
                    mtime = None
                if sig == self._last_poll_sig and mtime == self._last_log_mtime:
                    pass
                else:
                    self._last_poll_sig, self._last_log_mtime = sig, mtime
                    self._refresh_all()
        except Exception:
            logger.debug("poll failed", exc_info=True)
            try:
                self._request_refresh(50)
            except Exception:
                pass
        finally:
            self._poll_after_id = self.after(self._poll_interval_ms, self._poll)

    def _stop_polling(self) -> None:
        if self._poll_after_id:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        # Also cancel coalesced refresh
        if self._refresh_after is not None:
            try:
                self.after_cancel(self._refresh_after)
            except Exception:
                pass
            self._refresh_after = None
            self._refresh_queued = False
        if self._toast_after is not None:
            try:
                self.after_cancel(self._toast_after)
            except Exception:
                pass
            self._toast_after = None

    def _toast(self, msg: str, duration: int = 2500) -> None:
        self._footer_status.configure(text=msg)
        self.after(duration, lambda: self._footer_status.configure(text=""))

    def _on_close(self) -> None:
        if self.watcher and self.watcher.is_running():
            try:
                c = self.watcher.stop()
                if c:
                    messagebox.showwarning("Organizer", f"{c} file(s) were still settling and were not queued.\nThey will be caught on next startup scan.")
            except Exception:
                pass
        self._stop_polling()
        self.destroy()


def main() -> None:
    app = OrganizerGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
