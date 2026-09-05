from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Generator

try:
    from core.models import ProposedAction, ActionStatus
except ImportError:
    from .models import ProposedAction, ActionStatus


class QueueManager:
    """SQLite queue. Only this class touches the DB."""

    _TABLE = "queue"

    def __init__(self, db_path: str = "queue.db"):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=5.0)
            self._memory_conn.row_factory = sqlite3.Row
        self._init_db()

    def _new_connection(self) -> sqlite3.Connection:
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
        """Yield a connection. Shared locked one for :memory:, fresh for files."""
        if self._memory_conn is not None:
            with self._lock:
                yield self._memory_conn
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
        return ProposedAction.from_dict(dict(row) if isinstance(row, sqlite3.Row) else dict(row))

    def _init_db(self):
        """Create table + indexes if needed."""
        with self._connect() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._TABLE} (
                    id TEXT PRIMARY KEY, src_path TEXT NOT NULL, suggested_dest TEXT NOT NULL,
                    matched_rule TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                    resolved_at TEXT, filename TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL DEFAULT '',
                    error_message TEXT
                );""")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_status ON {self._TABLE}(status);")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_created_at ON {self._TABLE}(created_at);")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_src_path ON {self._TABLE}(src_path);")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_src_path_status ON {self._TABLE}(src_path, status);")
            conn.commit()

    def find_pending_by_path(self, src_path: str) -> Optional[ProposedAction]:
        with self._connect() as conn:
            cur = conn.execute(f"SELECT * FROM {self._TABLE} WHERE src_path = ? AND status = ? LIMIT 1", (src_path, ActionStatus.PENDING.value))
            row = cur.fetchone()
        return self._row_to_action(row) if row else None

    def has_pending_for_path(self, src_path: str) -> bool:
        return self.find_pending_by_path(src_path) is not None

    def add(self, action: ProposedAction, dedup: bool = True) -> Optional[str]:
        """Insert. With dedup, updates existing pending for same src_path instead of duplicating."""
        with self._lock:
            if dedup and action.src_path:
                existing = self.find_pending_by_path(action.src_path)
                if existing:
                    with self._connect() as conn:
                        conn.execute(f"UPDATE {self._TABLE} SET suggested_dest=?, matched_rule=?, confidence=?, filename=?, extension=?, error_message=? WHERE id=?",
                                     (action.suggested_dest, action.matched_rule, action.confidence, action.filename or existing.filename, action.extension or existing.extension, action.error_message, existing.id))
                        conn.commit()
                    return existing.id
            data = action.to_dict()
            with self._connect() as conn:
                conn.execute(f"INSERT OR REPLACE INTO {self._TABLE} (id, src_path, suggested_dest, matched_rule, confidence, status, created_at, resolved_at, filename, extension, error_message) VALUES (:id, :src_path, :suggested_dest, :matched_rule, :confidence, :status, :created_at, :resolved_at, :filename, :extension, :error_message)", data)
                conn.commit()
            return data["id"]

    def add_many(self, actions: List[ProposedAction], dedup: bool = True) -> None:
        if not actions:
            return
        if dedup:
            for a in actions:
                self.add(a, dedup=True)
            return
        rows = [a.to_dict() for a in actions]
        with self._connect() as conn:
            conn.executemany(f"INSERT OR REPLACE INTO {self._TABLE} (id, src_path, suggested_dest, matched_rule, confidence, status, created_at, resolved_at, filename, extension, error_message) VALUES (:id, :src_path, :suggested_dest, :matched_rule, :confidence, :status, :created_at, :resolved_at, :filename, :extension, :error_message)", rows)
            conn.commit()

    def get_pending(self, limit: int = 50) -> List[ProposedAction]:
        """Pending, newest first, paginated."""
        with self._connect() as conn:
            cur = conn.execute(f"SELECT * FROM {self._TABLE} WHERE status=? ORDER BY datetime(created_at) DESC LIMIT ?", (ActionStatus.PENDING.value, limit))
            rows = cur.fetchall()
        return [self._row_to_action(r) for r in rows]

    def get_by_id(self, action_id: str) -> Optional[ProposedAction]:
        with self._connect() as conn:
            cur = conn.execute(f"SELECT * FROM {self._TABLE} WHERE id=?", (action_id,))
            row = cur.fetchone()
        return self._row_to_action(row) if row else None

    def get_all(self, limit: int = 100, offset: int = 0, status: Optional[ActionStatus | str] = None) -> List[ProposedAction]:
        with self._connect() as conn:
            if status is not None:
                val = status.value if isinstance(status, ActionStatus) else str(status)
                cur = conn.execute(f"SELECT * FROM {self._TABLE} WHERE status=? ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?", (val, limit, offset))
            else:
                cur = conn.execute(f"SELECT * FROM {self._TABLE} ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?", (limit, offset))
            rows = cur.fetchall()
        return [self._row_to_action(r) for r in rows]

    def list_by_status(self, status: ActionStatus | str) -> List[ProposedAction]:
        return self.get_all(limit=1000, status=status)

    def count(self, status: Optional[ActionStatus | str] = None) -> int:
        with self._connect() as conn:
            if status is not None:
                val = status.value if isinstance(status, ActionStatus) else str(status)
                cur = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE} WHERE status=?", (val,))
            else:
                cur = conn.execute(f"SELECT COUNT(*) FROM {self._TABLE}")
            (n,) = cur.fetchone()
        return int(n)

    def count_pending(self) -> int:
        return self.count(ActionStatus.PENDING)

    def update_status(self, action_id: str, new_status: ActionStatus | str) -> bool:
        if isinstance(new_status, ActionStatus):
            status_val = new_status.value
        else:
            try:
                status_val = ActionStatus(str(new_status)).value
            except ValueError:
                status_val = str(new_status)
        resolved_at = None if status_val == ActionStatus.PENDING.value else datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE {self._TABLE} SET status=?, resolved_at=? WHERE id=?", (status_val, resolved_at, action_id))
            conn.commit()
            return cur.rowcount > 0

    def set_error(self, action_id: str, message: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE {self._TABLE} SET error_message=? WHERE id=?", (message, action_id))
            conn.commit()
            return cur.rowcount > 0

    def delete(self, action_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM {self._TABLE} WHERE id=?", (action_id,))
            conn.commit()
            return cur.rowcount > 0

    def clear(self, status: Optional[ActionStatus | str] = None) -> int:
        with self._connect() as conn:
            if status is not None:
                val = status.value if isinstance(status, ActionStatus) else str(status)
                cur = conn.execute(f"DELETE FROM {self._TABLE} WHERE status=?", (val,))
            else:
                cur = conn.execute(f"DELETE FROM {self._TABLE}")
            conn.commit()
            return cur.rowcount

    def clear_all(self) -> int:
        return self.clear()

    def close(self) -> None:
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
