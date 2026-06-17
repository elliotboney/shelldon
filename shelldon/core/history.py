"""core/history — the conversation-history store (AD-6/AD-5).

One sqlite file (`~/.shelldon/history.db`) in WAL mode: an ordered, timestamped
`messages` table with an FTS5 index for keyword recall. Core is the sole writer
(AD-5) and records each completed turn's `(owner, pet)` pair in one transaction
(the AD-6 "batched commit" — one commit per turn, not per row). Workers get a
read-only handle (`mode=ro`) — they can recall, never write.

Scope (binding): history substrate ONLY. Memory-ops + the markdown tree are Story
4.2; the vault/uid isolation is 4.3; injecting history into a prompt is 4.4; the
`learnings` table + dream cycle are later. The schema is single-owner but shaped so
a `chat_id`/`user_id` key is a non-breaking `ALTER TABLE ADD COLUMN` later (AD-13).
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger("shelldon.core.history")

#: Default store location — one file beside the state checkpoint. Always injectable;
#: tests pass a `tmp_path` db and never touch real `$HOME`.
DEFAULT_HISTORY_PATH = Path.home() / ".shelldon" / "history.db"

#: Single-owner schema. `id` gives stable insertion order; `ts` is ISO-8601 UTC.
#: A later story adds `chat_id`/`user_id` as NULLABLE columns (non-breaking — AD-13);
#: do NOT make any such column NOT NULL or it forces a destructive migration.
#: FTS5 mirrors `content` via external-content + an insert trigger (append-only here).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY,
    turn_id TEXT,
    role    TEXT NOT NULL CHECK (role IN ('owner', 'pet')),
    content TEXT NOT NULL,
    ts      TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Run the schema script (module-level so tests can simulate a missing FTS5)."""
    conn.executescript(_SCHEMA)


def _recent(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    """The last `n` messages in chronological (oldest→newest) order."""
    cur = conn.execute(
        "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (n,),
    )
    return cur.fetchall()


def _search(conn: sqlite3.Connection, query: str, n: int) -> list[sqlite3.Row]:
    """FTS5 keyword recall over message content, most-relevant first."""
    cur = conn.execute(
        "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid "
        "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, n),
    )
    return cur.fetchall()


class HistoryReader:
    """Read-only view workers use (AD-5): recall queries, no write path. The
    connection is opened `mode=ro`, so even a raw write raises at the sqlite layer."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def recent(self, n: int = 20) -> list[sqlite3.Row]:
        return _recent(self._conn, n)

    def search(self, query: str, n: int = 20) -> list[sqlite3.Row]:
        return _search(self._conn, query, n)

    def close(self) -> None:
        self._conn.close()


def open_readonly(path) -> HistoryReader:
    """Open `path` read-only (`file:…?mode=ro`) — the handle a worker gets. A write
    through it raises `sqlite3.OperationalError`."""
    uri = f"file:{Path(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return HistoryReader(conn)


class HistoryStore:
    """The writer (core-owned, single writer per AD-5). Opens WAL, ensures the
    schema, records turns, and reads back recent/recall."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path) -> "HistoryStore":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        cls._ensure_schema(conn)
        return cls(conn)

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        try:
            _apply_schema(conn)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise RuntimeError(
                    "this sqlite build lacks the FTS5 module — conversation recall "
                    "(AD-6) requires it; rebuild/enable sqlite with FTS5"
                ) from exc
            raise

    def record_turn(self, turn_id: str | None, owner_text: str, pet_text: str, now: datetime) -> None:
        """Append the owner message then the pet reply in ONE transaction (one commit
        per turn — AD-6 batched). Both share the turn's timestamp; `id` orders them."""
        ts = now.isoformat()
        with self._conn:  # transaction: commit once, both rows or neither
            self._conn.execute(
                "INSERT INTO messages (turn_id, role, content, ts) VALUES (?, 'owner', ?, ?)",
                (turn_id, owner_text, ts),
            )
            self._conn.execute(
                "INSERT INTO messages (turn_id, role, content, ts) VALUES (?, 'pet', ?, ?)",
                (turn_id, pet_text, ts),
            )

    def recent(self, n: int = 20) -> list[sqlite3.Row]:
        return _recent(self._conn, n)

    def search(self, query: str, n: int = 20) -> list[sqlite3.Row]:
        return _search(self._conn, query, n)

    def close(self) -> None:
        self._conn.close()
