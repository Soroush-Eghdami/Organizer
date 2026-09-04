from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Generator

try:
    from core.models import ProposedAction, ActionStatus
except ImportError:  # when imported as `organizer.core.queue_manager`
    from .models import ProposedAction, ActionStatus


class QueueManager:
    """
    Owns all persistence for the confirmation queue.
    Nothing outside this class should touch the SQLite file directly —
    that keeps mover.py, cli_app.py, gui_app.py all decoupled from
    the storage implementation (you could swap SQLite for something
    else later without touching them).
    """

    _TABLE = "queue"

    def __init__(self, db_path: str = "queue.db"):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        # For :memory: each new connection would create a fresh DB, so keep one
        # persistent connection. For file DBs we create a new connection per call
        # (safer for threading / watcher + gui concurrency).
        self._memory_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=5.0)
            self._memory_conn.row_factory = sqlite3.Row
        self._init_db()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _new_connection(self) -> sqlite3.Connection:
        """Create a fresh connection (never called for :memory:)."""
        parent = Path(self.db_path).parent
        if str(parent) not in (".", ""):
            parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.Error:
            pass
        return conn

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager that yields a connection.
        For :memory: it yields the shared connection (never closed) under lock.
        For file DBs it creates a new connection per call and closes it.
        """
        if self._memory_conn is not None:
            # Serialize access to the single shared :memory: connection — watchdog
            # fires callbacks on its own thread, so concurrent add/get_pending can
            # race without this.
            with self._lock:
                yield self._memory_conn
                # commit is handled by callers; ensure transaction committed
                try:
                    self._memory_conn.commit()
                except sqlite3.Error:
                    pass
            return

        conn = self._new_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _row_to_action(self, row: sqlite3.Row | dict) -> ProposedAction:
        """Convert a SQLite row (or dict) into a ProposedAction."""
        # sqlite3.Row behaves like a dict but ProposedAction.from_dict expects dict
        if isinstance(row, sqlite3.Row):
            data = dict(row)
        else:
            data = dict(row)
        return ProposedAction.from_dict(data)

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def _init_db(self):
        """
        Create the table if it doesn't exist.
        Schema mirrors ProposedAction fields. `id` (UUID string) is primary key.
        Uses `CREATE TABLE IF NOT EXISTS` so this is safe to call on every start.
        """
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._TABLE} (
                    id              TEXT PRIMARY KEY,
                    src_path        TEXT NOT NULL,
                    suggested_dest  TEXT NOT NULL,
                    matched_rule    TEXT NOT NULL,
                    confidence      REAL NOT NULL DEFAULT 1.0,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    created_at      TEXT NOT NULL,
                    resolved_at     TEXT,
                    filename        TEXT NOT NULL DEFAULT '',
                    extension       TEXT NOT NULL DEFAULT '',
                    error_message   TEXT
                );
                """
            )
            # Helpful indexes for the hot query (pending queue) and sorting
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_status ON {self._TABLE}(status);"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_created_at ON {self._TABLE}(created_at);"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_src_path ON {self._TABLE}(src_path);"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_src_path_status ON {self._TABLE}(src_path, status);"
            )
            conn.commit()

    # ------------------------------------------------------------------ #
    # Deduplication helpers
    # ------------------------------------------------------------------ #
    def find_pending_by_path(self, src_path: str) -> Optional[ProposedAction]:
        """Return the pending entry for this exact src_path, if any."""
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT * FROM {self._TABLE} WHERE src_path = ? AND status = ? LIMIT 1",
                (src_path, ActionStatus.PENDING.value),
            )
            row = cur.fetchone()
        return self._row_to_action(row) if row else None

    def has_pending_for_path(self, src_path: str) -> bool:
        """True if a pending action already exists for this path."""
        return self.find_pending_by_path(src_path) is not None

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def add(self, action: ProposedAction, dedup: bool = True) -> Optional[str]:
        """
        Insert a new ProposedAction into the queue.
        Converts dataclass fields into a SQL INSERT via ProposedAction.to_dict().

        dedup=True (default) prevents duplicate pending entries for the same
        src_path — watcher debounce isn't perfect and can fire twice for the
        same file. Since id is a fresh UUID, INSERT OR REPLACE would not
        catch this (it only replaces on matching id). We instead check for
        an existing PENDING row with the same src_path and skip/update it.

        Returns the inserted id, or the existing pending id if deduped, or
        None if no insert happened. Caller can ignore the return value for
        backwards compatibility.
        """
        # Hold lock across check-then-act to prevent race where two watcher
        # threads both see no pending and both insert duplicates.
        # RLock allows nested find_pending_by_path (which also uses _connect/lock).
        with self._lock:
            if dedup and action.src_path:
                existing = self.find_pending_by_path(action.src_path)
                if existing:
                    # Update the existing pending entry in place rather than
                    # creating a duplicate. Keeps queue tidy — user sees one
                    # entry, not two for the same file.
                    with self._connect() as conn:
                        conn.execute(
                            f"""
                            UPDATE {self._TABLE}
                            SET suggested_dest = ?,
                                matched_rule   = ?,
                                confidence     = ?,
                                filename       = ?,
                                extension      = ?,
                                error_message  = ?
                            WHERE id = ?
                            """,
                            (
                                action.suggested_dest,
                                action.matched_rule,
                                action.confidence,
                                action.filename or existing.filename,
                                action.extension or existing.extension,
                                action.error_message,
                                existing.id,
                            ),
                        )
                        conn.commit()
                    return existing.id

            data = action.to_dict()
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO {self._TABLE}
                        (id, src_path, suggested_dest, matched_rule, confidence,
                         status, created_at, resolved_at, filename, extension, error_message)
                    VALUES
                        (:id, :src_path, :suggested_dest, :matched_rule, :confidence,
                         :status, :created_at, :resolved_at, :filename, :extension, :error_message)
                    """,
                    data,
                )
                conn.commit()
            return data["id"]

    def add_many(self, actions: List[ProposedAction], dedup: bool = True) -> None:
        """Batch insert — useful when classifier processes a burst of events."""
        if not actions:
            return
        if dedup:
            # Deduplicate one-by-one so we reuse the pending-path check.
            for a in actions:
                self.add(a, dedup=True)
            return
        rows = [a.to_dict() for a in actions]
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {self._TABLE}
                    (id, src_path, suggested_dest, matched_rule, confidence,
                     status, created_at, resolved_at, filename, extension, error_message)
                VALUES
                    (:id, :src_path, :suggested_dest, :matched_rule, :confidence,
                     :status, :created_at, :resolved_at, :filename, :extension, :error_message)
                """,
                rows,
            )
            conn.commit()

    def get_pending(self, limit: int = 50) -> List[ProposedAction]:
        """
        Fetch pending items, most recent first, paginated.
        This is the method both CLI and GUI will call to show
        the review queue — we do NOT load everything into memory at once.
        """
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                SELECT * FROM {self._TABLE}
                WHERE status = ?
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (ActionStatus.PENDING.value, limit),
            )
            rows = cur.fetchall()
        return [self._row_to_action(r) for r in rows]

    def get_by_id(self, action_id: str) -> Optional[ProposedAction]:
        """Fetch a single item by id, or None if not found."""
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT * FROM {self._TABLE} WHERE id = ?", (action_id,)
            )
            row = cur.fetchone()
        return self._row_to_action(row) if row else None

    def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
        status: Optional[ActionStatus | str] = None,
    ) -> List[ProposedAction]:
        """
        Fetch all items (optionally filtered by status), newest first.
        Useful for activity log / history views.
        """
        with self._connect() as conn:
            if status is not None:
                val = status.value if isinstance(status, ActionStatus) else str(status)
                cur = conn.execute(
                    f"""
                    SELECT * FROM {self._TABLE}
                    WHERE status = ?
                    ORDER BY datetime(created_at) DESC
                    LIMIT ? OFFSET ?
                    """,
                    (val, limit, offset),
                )
            else:
                cur = conn.execute(
                    f"""
                    SELECT * FROM {self._TABLE}
                    ORDER BY datetime(created_at) DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                )
            rows = cur.fetchall()
        return [self._row_to_action(r) for r in rows]

    def list_by_status(self, status: ActionStatus | str) -> List[ProposedAction]:
        """Convenience wrapper for get_all filtered by status."""
        return self.get_all(limit=1000, status=status)

    def count(self, status: Optional[ActionStatus | str] = None) -> int:
        """Count items, optionally filtered by status."""
        with self._connect() as conn:
            if status is not None:
                val = status.value if isinstance(status, ActionStatus) else str(status)
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {self._TABLE} WHERE status = ?", (val,)
                )
            else:
                cur = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE}")
            (n,) = cur.fetchone()
        return int(n)

    def count_pending(self) -> int:
        """Shorthand for count(status=PENDING)."""
        return self.count(ActionStatus.PENDING)

    def update_status(self, action_id: str, new_status: ActionStatus | str) -> bool:
        """
        Called when the user approves/rejects an item in CLI/GUI or when
        mover.py marks an item as moved.
        Simple UPDATE ... WHERE id = ?. Also maintains resolved_at.
        Returns True if a row was updated, False if id not found.
        """
        if isinstance(new_status, ActionStatus):
            status_val = new_status.value
        else:
            # coerce / validate string -> enum value
            try:
                status_val = ActionStatus(str(new_status)).value
            except ValueError:
                status_val = str(new_status)

        # resolved_at is set when leaving PENDING, cleared when going back to PENDING
        if status_val == ActionStatus.PENDING.value:
            resolved_at = None
        else:
            resolved_at = datetime.now().isoformat()

        with self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE {self._TABLE}
                SET status = ?, resolved_at = ?
                WHERE id = ?
                """,
                (status_val, resolved_at, action_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_error(self, action_id: str, message: str) -> bool:
        """Attach an error message to an item (e.g. move failed)."""
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE {self._TABLE} SET error_message = ? WHERE id = ?",
                (message, action_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete(self, action_id: str) -> bool:
        """
        Fully remove an item (e.g. after mover.py has successfully moved it
        and you don't want it cluttering the history, or for test cleanup).
        Returns True if a row was deleted.
        """
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM {self._TABLE} WHERE id = ?", (action_id,))
            conn.commit()
            return cur.rowcount > 0

    def clear(self, status: Optional[ActionStatus | str] = None) -> int:
        """
        Delete all items, or only those with a given status.
        Returns number of rows deleted. Handy for tests / 'clear history'.
        """
        with self._connect() as conn:
            if status is not None:
                val = status.value if isinstance(status, ActionStatus) else str(status)
                cur = conn.execute(f"DELETE FROM {self._TABLE} WHERE status = ?", (val,))
            else:
                cur = conn.execute(f"DELETE FROM {self._TABLE}")
            conn.commit()
            return cur.rowcount

    # alias kept for backwards-compat if someone calls clear_all()
    def clear_all(self) -> int:
        return self.clear()

    def close(self) -> None:
        """Close the shared :memory: connection if present."""
        if self._memory_conn is not None:
            try:
                self._memory_conn.close()
            except sqlite3.Error:
                pass
            self._memory_conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
