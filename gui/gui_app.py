from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

# Ensure project root on sys.path for direct execution
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

logger = logging.getLogger(__name__)

# Theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class OrganizerGUI(ctk.CTk):
    """Powerful, fast, easy — thin GUI shell over the same core as CLI."""

    def __init__(
        self,
        watch_path: str | None = None,
        organize_root: str | None = None,
        db_path: str = "queue.db",
        rules_path: str | None = None,
        debounce: float = 2.0,
    ):
        super().__init__()

        self.title("Organizer — File Sorter")
        self.geometry("1100x720")
        self.minsize(900, 600)

        # Core wiring (same as CLI, no business logic here)
        self.db_path = db_path
        self.rules_path = rules_path
        self.qm = QueueManager(db_path=db_path)
        self.classifier = Classifier(rules_path=rules_path, queue_manager=self.qm)
        # organize_root defaults to ./organized
        self.organize_root = str(Path(organize_root).resolve()) if organize_root else str((Path.cwd() / "organized").resolve())
        self.mover = Mover(organize_root=self.organize_root, queue_manager=self.qm)
        self.watcher: FolderWatcher | None = None
        self._watch_path = watch_path or ""
        self._debounce = debounce

        # Toast polling
        self._poll_after_id: str | None = None

        self._build_ui()
        self._refresh_all()

        # Auto-start watch if path provided
        if watch_path and Path(watch_path).is_dir():
            self._watch_entry.delete(0, "end")
            self._watch_entry.insert(0, watch_path)
            self._organize_entry.delete(0, "end")
            self._organize_entry.insert(0, self.organize_root)
            self.start_watching()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        # Top controls
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=12, pady=(12, 6))

        # Watch path
        ctk.CTkLabel(top, text="Watch:", width=60).grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self._watch_entry = ctk.CTkEntry(top, placeholder_text="D:\\Downloads", width=340)
        self._watch_entry.grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        ctk.CTkButton(top, text="Browse", width=80, command=self._browse_watch).grid(row=0, column=2, padx=6)

        # Organize root
        ctk.CTkLabel(top, text="Organize:", width=60).grid(row=0, column=3, padx=6, pady=6, sticky="w")
        self._organize_entry = ctk.CTkEntry(top, placeholder_text="D:\\Organized", width=340)
        self._organize_entry.grid(row=0, column=4, padx=6, pady=6, sticky="ew")
        self._organize_entry.insert(0, self.organize_root)
        ctk.CTkButton(top, text="Browse", width=80, command=self._browse_organize).grid(row=0, column=5, padx=6)

        # Second row: recursive + watch controls + scan
        ctk.CTkLabel(top, text="Options:", width=60).grid(row=1, column=0, padx=6, pady=6, sticky="w")
        self._recursive_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(top, text="Recursive", variable=self._recursive_var).grid(row=1, column=1, padx=6, sticky="w")

        self._watch_btn = ctk.CTkButton(top, text="▶ Start Watching", width=140, fg_color="#1f6aa5", command=self.toggle_watching)
        self._watch_btn.grid(row=1, column=2, padx=6)

        self._scan_btn = ctk.CTkButton(top, text="Scan Now", width=100, fg_color="#555555", command=self.scan_now)
        self._scan_btn.grid(row=1, column=3, padx=6)

        self._status_dot = ctk.CTkLabel(top, text="● idle", text_color="#888888", width=100)
        self._status_dot.grid(row=1, column=4, padx=6)

        # Stats row
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

        # Bulk actions
        bulk = ctk.CTkFrame(self)
        bulk.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(bulk, text="Approve All Pending", width=160, fg_color="#16a34a", hover_color="#15803d", command=self.approve_all).pack(side="left", padx=6, pady=6)
        ctk.CTkButton(bulk, text="Reject All Pending", width=160, fg_color="#dc2626", hover_color="#b91c1c", command=self.reject_all).pack(side="left", padx=6)
        ctk.CTkButton(bulk, text="▶ Move Approved", width=160, fg_color="#2563eb", hover_color="#1d4ed8", command=self.move_approved).pack(side="left", padx=6)
        ctk.CTkButton(bulk, text="↻ Refresh", width=100, fg_color="#4b5563", command=self._refresh_all).pack(side="right", padx=6)
        ctk.CTkButton(bulk, text="Clear Rejected", width=120, fg_color="#6b7280", command=self.clear_rejected).pack(side="right", padx=6)

        # Main tabs
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=6)
        self.tabs.add("Pending")
        self.tabs.add("Approved")
        self.tabs.add("History")
        self.tabs.add("Logs")

        # Pending scroll
        self.pending_scroll = ctk.CTkScrollableFrame(self.tabs.tab("Pending"), label_text="Awaiting your review — choose Approve or Reject per file")
        self.pending_scroll.pack(fill="both", expand=True, padx=6, pady=6)

        # Approved scroll
        self.approved_scroll = ctk.CTkScrollableFrame(self.tabs.tab("Approved"), label_text="Ready to move")
        self.approved_scroll.pack(fill="both", expand=True, padx=6, pady=6)

        # History: moved + rejected
        self.history_scroll = ctk.CTkScrollableFrame(self.tabs.tab("History"), label_text="Moved / Rejected (newest first)")
        self.history_scroll.pack(fill="both", expand=True, padx=6, pady=6)

        # Logs
        self.log_text = ctk.CTkTextbox(self.tabs.tab("Logs"), wrap="word", height=200)
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text.insert("1.0", "Activity log — tail of logs/activity.log\n\n")
        self.log_text.configure(state="disabled")

        # Footer
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(footer, text="Tip: drop files into the watched folder — they appear in Pending after ~2.5s (debounce). Use Scan Now if you Ctrl+C mid-settle.", text_color="#9ca3af", font=("Segoe UI", 11)).pack(side="left")
        self._footer_status = ctk.CTkLabel(footer, text="", text_color="#4ade80")
        self._footer_status.pack(side="right")

        top.grid_columnconfigure(1, weight=1)
        top.grid_columnconfigure(4, weight=1)

    # ------------------------------------------------------------------ #
    # Watch controls
    # ------------------------------------------------------------------ #
    def _browse_watch(self) -> None:
        d = filedialog.askdirectory(title="Choose folder to watch")
        if d:
            self._watch_entry.delete(0, "end")
            self._watch_entry.insert(0, d)

    def _browse_organize(self) -> None:
        d = filedialog.askdirectory(title="Choose organize destination root")
        if d:
            self._organize_entry.delete(0, "end")
            self._organize_entry.insert(0, d)
            # Recreate mover with new root
            self.organize_root = d
            self.mover = Mover(organize_root=d, queue_manager=self.qm)

    def _scan_now(self, watch_path: str) -> int:
        """Scan existing files not yet pending — same logic as CLI startup scan."""
        from cli.cli_app import scan_existing_files  # reuse CLI scan

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
        if n:
            self._toast(f"Scanned {n} new file(s)")
        else:
            self._toast("Scan: nothing new")
        self._refresh_all()

    def _make_on_file_ready(self):
        """Bridge for FolderWatcher — runs on watcher thread, so schedule GUI refresh via after."""
        def on_file_ready(event):
            try:
                action = self.classifier.classify(event)
                if action is None:
                    return
                self.qm.add(action, dedup=True)
                # Schedule refresh on main thread
                self.after(0, self._refresh_all)
                self.after(0, lambda: self._toast(f"Queued {action.filename} → {action.suggested_dest}"))
            except Exception:
                logger.exception("GUI on_file_ready failed for %s", getattr(event, "src_path", "?"))
        return on_file_ready

    def start_watching(self) -> None:
        wp = self._watch_entry.get().strip()
        if not wp:
            messagebox.showwarning("Watch", "Please select a watch folder.")
            return
        if not Path(wp).is_dir():
            messagebox.showerror("Watch", f"Folder does not exist:\n{wp}")
            return
        org = self._organize_entry.get().strip()
        if org:
            self.organize_root = org
            self.mover = Mover(organize_root=org, queue_manager=self.qm)
        else:
            org = self.organize_root

        # Scan first (catches files while app was closed + settle-cancel gap)
        self._scan_now(wp)

        if self.watcher and self.watcher.is_running():
            messagebox.showinfo("Watch", "Already watching.")
            return

        try:
            self.watcher = FolderWatcher(
                watch_path=wp,
                on_file_ready=self._make_on_file_ready(),
                recursive=self._recursive_var.get(),
                debounce_seconds=self._debounce,
            )
            self.watcher.start()
            self._watch_btn.configure(text="■ Stop Watching", fg_color="#dc2626", hover_color="#b91c1c", command=self.stop_watching)
            self._status_dot.configure(text="● watching", text_color="#4ade80")
            self._toast(f"Watching {wp}")
            logger.info("GUI watching %s", wp)
            self._start_polling()
        except Exception as e:
            messagebox.showerror("Watch", str(e))
            logger.exception("GUI start_watching failed")

    def stop_watching(self) -> None:
        if not self.watcher:
            return
        try:
            cancelled = self.watcher.stop()
            if cancelled:
                self._toast(f"{cancelled} file(s) were still settling — rescan on next start")
                messagebox.showwarning(
                    "Watch stopped",
                    f"{cancelled} file(s) were still settling and were not queued.\nThey remain in the watch folder and will be caught on the next startup scan.",
                )
        except Exception:
            logger.exception("GUI stop failed")
        finally:
            self.watcher = None
            self._watch_btn.configure(text="▶ Start Watching", fg_color="#1f6aa5", hover_color="#2563eb", command=self.start_watching)
            self._status_dot.configure(text="● idle", text_color="#888888")
            self._stop_polling()

    def toggle_watching(self) -> None:
        if self.watcher and self.watcher.is_running():
            self.stop_watching()
        else:
            self.start_watching()

    # ------------------------------------------------------------------ #
    # Queue actions
    # ------------------------------------------------------------------ #
    def approve_all(self) -> None:
        pend = self.qm.get_pending(limit=1000)
        if not pend:
            self._toast("Nothing pending to approve")
            return
        for a in pend:
            self.qm.update_status(a.id, ActionStatus.APPROVED)
        self._toast(f"Approved {len(pend)}")
        self._refresh_all()

    def reject_all(self) -> None:
        pend = self.qm.get_pending(limit=1000)
        if not pend:
            self._toast("Nothing pending to reject")
            return
        for a in pend:
            self.qm.update_status(a.id, ActionStatus.REJECTED)
        self._toast(f"Rejected {len(pend)}")
        self._refresh_all()

    def clear_rejected(self) -> None:
        n = self.qm.clear(status=ActionStatus.REJECTED)
        self._toast(f"Cleared {n} rejected")
        self._refresh_all()

    def move_approved(self) -> None:
        approved = self.qm.count(status=ActionStatus.APPROVED)
        if approved == 0:
            self._toast("No approved items to move")
            return
        # Run mover off UI thread to keep GUI responsive
        self._toast(f"Moving {approved} file(s)...")
        threading.Thread(target=self._move_thread, daemon=True).start()

    def _move_thread(self) -> None:
        try:
            summary = self.mover.move_all_approved()
            self.after(0, lambda: self._on_move_done(summary))
        except Exception as e:
            logger.exception("GUI move failed")
            self.after(0, lambda: messagebox.showerror("Move", str(e)))

    def _on_move_done(self, summary: dict) -> None:
        msg = f"Moved {summary.get('moved',0)}, Failed {summary.get('failed',0)}"
        if summary.get("failed"):
            messagebox.showwarning("Move", msg + "\nCheck logs/activity.log for details.")
        else:
            self._toast(msg)
        self._refresh_all()

    def _approve_one(self, action_id: str) -> None:
        self.qm.update_status(action_id, ActionStatus.APPROVED)
        self._refresh_all()

    def _reject_one(self, action_id: str) -> None:
        self.qm.update_status(action_id, ActionStatus.REJECTED)
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def _clear_scroll(self, scroll: ctk.CTkScrollableFrame) -> None:
        for w in scroll.winfo_children():
            w.destroy()

    def _add_row(
        self,
        parent: ctk.CTkScrollableFrame,
        action,
        show_approve: bool = False,
        show_reject: bool = False,
    ) -> None:
        row = ctk.CTkFrame(parent)
        row.pack(fill="x", padx=6, pady=4)

        # Icon by extension
        icon_map = {
            ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️",
            ".mp4": "🎬", ".mkv": "🎬",
            ".pdf": "📄", ".docx": "📄", ".txt": "📄",
            ".mp3": "🎵", ".flac": "🎵",
        }
        icon = icon_map.get((action.extension or "").lower(), "📦" if "fallback" in (action.matched_rule or "") else "📄")

        header = ctk.CTkLabel(row, text=f"{icon}  {action.filename or action.src_path}", font=("Segoe UI", 12, "bold"), anchor="w")
        header.pack(fill="x", padx=8, pady=(6, 0))

        meta = f"→ {action.suggested_dest}  •  {action.matched_rule}  •  {action.extension or '-'}  •  {action.id[:8]}"
        ctk.CTkLabel(row, text=meta, text_color="#9ca3af", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=8)

        src = ctk.CTkLabel(row, text=action.src_path, text_color="#6b7280", font=("Segoe UI", 10), anchor="w")
        src.pack(fill="x", padx=8, pady=(0, 4))

        if action.error_message:
            ctk.CTkLabel(row, text=f"⚠ {action.error_message}", text_color="#f87171", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=8, pady=(0, 4))

        if show_approve or show_reject:
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.pack(fill="x", padx=8, pady=(0, 6))
            if show_approve:
                ctk.CTkButton(btns, text="✓ Approve", width=90, height=26, fg_color="#16a34a", hover_color="#15803d", command=lambda aid=action.id: self._approve_one(aid)).pack(side="left", padx=4)
            if show_reject:
                ctk.CTkButton(btns, text="✕ Reject", width=90, height=26, fg_color="#dc2626", hover_color="#b91c1c", command=lambda aid=action.id: self._reject_one(aid)).pack(side="left", padx=4)
            # Open folder button
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

    def _refresh_all(self) -> None:
        # Stats
        try:
            pending_n = self.qm.count_pending()
            approved_n = self.qm.count(status=ActionStatus.APPROVED)
            moved_n = self.qm.count(status=ActionStatus.MOVED)
            rejected_n = self.qm.count(status=ActionStatus.REJECTED)
            self._pending_label.configure(text=f"Pending: {pending_n}")
            self._approved_label.configure(text=f"Approved: {approved_n}")
            self._moved_label.configure(text=f"Moved: {moved_n}")
            self._rejected_label.configure(text=f"Rejected: {rejected_n}")
        except Exception:
            pass

        # Pending
        try:
            self._clear_scroll(self.pending_scroll)
            pend = self.qm.get_pending(limit=100)
            if not pend:
                ctk.CTkLabel(self.pending_scroll, text="No pending — drop files into the watch folder or click Scan Now.", text_color="#9ca3af").pack(pady=20)
            else:
                for a in pend:
                    self._add_row(self.pending_scroll, a, show_approve=True, show_reject=True)
        except Exception:
            logger.exception("refresh pending failed")

        # Approved
        try:
            self._clear_scroll(self.approved_scroll)
            approved = self.qm.list_by_status(ActionStatus.APPROVED)
            if not approved:
                ctk.CTkLabel(self.approved_scroll, text="No approved — approve from Pending then click Move Approved.", text_color="#9ca3af").pack(pady=20)
            else:
                for a in approved:
                    self._add_row(self.approved_scroll, a, show_approve=False, show_reject=False)
        except Exception:
            logger.exception("refresh approved failed")

        # History
        try:
            self._clear_scroll(self.history_scroll)
            all_items = self.qm.get_all(limit=80)
            # Filter to moved/rejected, pending/approved already shown
            hist = [x for x in all_items if x.status in (ActionStatus.MOVED, ActionStatus.REJECTED)]
            if not hist:
                ctk.CTkLabel(self.history_scroll, text="No history yet.", text_color="#9ca3af").pack(pady=20)
            else:
                for a in hist:
                    self._add_row(self.history_scroll, a)
        except Exception:
            logger.exception("refresh history failed")

        # Logs tail
        try:
            log_path = Path(__file__).resolve().parent.parent / "logs" / "activity.log"
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
                self.log_text.configure(state="normal")
                self.log_text.delete("1.0", "end")
                self.log_text.insert("1.0", "\n".join(lines) or "(empty)")
                self.log_text.configure(state="disabled")
                self.log_text.see("end")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Polling & toast
    # ------------------------------------------------------------------ #
    def _start_polling(self) -> None:
        self._stop_polling()
        self._poll()

    def _poll(self) -> None:
        try:
            self._refresh_all()
        finally:
            self._poll_after_id = self.after(1000, self._poll)

    def _stop_polling(self) -> None:
        if self._poll_after_id:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def _toast(self, msg: str, duration: int = 2500) -> None:
        self._footer_status.configure(text=msg)
        self.after(duration, lambda: self._footer_status.configure(text=""))

    def _on_close(self) -> None:
        # Stop watcher with warning logic (same as CLI)
        if self.watcher and self.watcher.is_running():
            try:
                cancelled = self.watcher.stop()
                if cancelled:
                    messagebox.showwarning(
                        "Organizer",
                        f"{cancelled} file(s) were still settling and were not queued.\nThey will be caught on next startup scan.",
                    )
            except Exception:
                pass
        self._stop_polling()
        self.destroy()


def main() -> None:
    app = OrganizerGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
