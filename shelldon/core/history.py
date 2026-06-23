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
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("shelldon.core.history")

#: Default lifetime of a parked RISKY-tool approval (Story 9.3): a tap older than this is
#: dropped, never executed (AC4). Module-level so tests can pass a tiny ttl.
DEFAULT_APPROVAL_TTL_S = 3600

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

-- Story 6.1: the learnings table (AD-6) — the raw, queryable capture buffer the dream
-- cycle (6.2) later classifies/promotes/prunes. `pattern_key` is the dedup identity
-- (nullable: an anonymous observation never dedups). `status` lifecycle is
-- pending -> promoted | pruned; 6.1 only ever writes/refreshes `pending`.
CREATE TABLE IF NOT EXISTS learnings (
    id               INTEGER PRIMARY KEY,
    pattern_key      TEXT,
    observation      TEXT NOT NULL,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'promoted', 'pruned')),
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);
-- UNIQUE PARTIAL index: a non-NULL pattern_key is unique (so dedup is enforced by the DB,
-- not a non-atomic SELECT-then-UPDATE), while NULL keys are exempt — anonymous learnings
-- always insert. This index is the conflict target for capture_learning's atomic UPSERT
-- (closes the 6.1-review TOCTOU: a second writer (the 6.2 dream) can't duplicate or lose a
-- row between a check and a write).
CREATE UNIQUE INDEX IF NOT EXISTS learnings_pattern_key_uq ON learnings(pattern_key) WHERE pattern_key IS NOT NULL;

-- Story 9.3: parked RISKY-tool approvals. When the worker pauses on a RISKY call it ends
-- the turn; core stashes the resumable agent state here keyed by turn id (additive table,
-- same CREATE-IF-NOT-EXISTS convention). `state_blob` is the msgpack of (messages, call).
-- A tap consumes (take+delete) the row; an expired/absent row never executes (AC4).
CREATE TABLE IF NOT EXISTS pending_approvals (
    turn_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state_blob BLOB NOT NULL
);

-- Story 9.4: parked TOOL PROMOTIONS, parallel to 9.3's pending_approvals (kept SEPARATE so 9.3's
-- resume table/signatures stay UNTOUCHED). When a self-coded tool passes its gate, core parks a
-- pending promotion keyed by turn id; the owner's Approve tap promotes the staged module to the
-- live dir. No blob — the staged files live on disk; only the `tool_name` (a safe module stem)
-- is needed to promote/discard. A tap dispatches by which table holds the turn id.
CREATE TABLE IF NOT EXISTS pending_promotions (
    turn_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    tool_name  TEXT NOT NULL
);

-- Story 9.5: the self-coded-tool health ledger (AD-8 quarantine). The worker reports a
-- self-coded tool that errored on import/run on its Result; core increments `strikes` here
-- (additive CREATE-IF-NOT-EXISTS, atomic UPSERT like `learnings`). At the strike threshold
-- core moves the live module to tools-quarantine/ — so a repeatedly-bad tool never wedges or
-- log-spams the worker. Keyed by the tool's safe module stem.
CREATE TABLE IF NOT EXISTS tool_health (
    name      TEXT PRIMARY KEY,
    strikes   INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL
);
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

    def capture_learning(self, observation: str, pattern_key: str | None, now: datetime) -> None:
        """Record (or fold into an existing) a hot-path learning (Story 6.1, AD-6) — single
        writer, one commit, ONE atomic statement. Dedup is by `pattern_key` ONLY: a matching
        key increments `recurrence_count`, refreshes `last_seen`, and resets `status='pending'`
        (so a recurring-but-already-promoted/pruned learning re-enters the dream queue). A
        `None` (or blank) `pattern_key` always inserts a fresh row. An empty/whitespace
        `observation` is skipped (a useless row) — logged, never written, never raised.

        The insert-or-increment is an atomic UPSERT against the unique partial index — not a
        SELECT-then-UPDATE — so a second writer (the 6.2 dream cycle) can neither duplicate a
        row nor lose one in a check-to-write gap (6.1-review TOCTOU fix)."""
        obs = (observation or "").strip()
        if not obs:
            log.warning("capture_learning: empty observation; skipping")
            return
        key = (pattern_key or "").strip() or None  # blank key -> None (no blank dedup bucket)
        ts = now.isoformat()
        with self._conn:  # one commit (AD-6 batched)
            # A NULL key is exempt from the partial unique index, so it never conflicts (always
            # inserts); a non-NULL key conflicts on a repeat and folds via DO UPDATE. Atomic.
            self._conn.execute(
                "INSERT INTO learnings (pattern_key, observation, recurrence_count, status, "
                "first_seen, last_seen) VALUES (?, ?, 1, 'pending', ?, ?) "
                "ON CONFLICT(pattern_key) WHERE pattern_key IS NOT NULL DO UPDATE SET "
                "recurrence_count = recurrence_count + 1, last_seen = excluded.last_seen, "
                "status = 'pending'",
                (key, obs, ts, ts),
            )

    def pending_learnings(self, limit: int = 50) -> list[sqlite3.Row]:
        """The `pending` learnings the dream cycle (Story 6.2) reviews — impact-first
        (`recurrence_count` desc), bounded. A CORE read (the dream's prompt is built in core),
        so no read-only-worker handle is needed."""
        cur = self._conn.execute(
            "SELECT id, pattern_key, observation, recurrence_count, first_seen, last_seen "
            "FROM learnings WHERE status = 'pending' "
            "ORDER BY recurrence_count DESC, id ASC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    def resolve_learning(self, id: int, status: str) -> None:
        """Transition a still-`pending` learning to `promoted` or `pruned` (Story 6.2) — a
        SOFT status change in one commit, never a DELETE (a re-recurring pruned learning resets
        to pending via the 6.1 UPSERT). Only a row that is currently `pending` is moved; an
        absent/already-resolved `id` is a 0-row no-op (logged, never raised). The `status` CHECK
        constraint rejects an out-of-set value at the DB."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE learnings SET status = ? WHERE id = ? AND status = 'pending'",
                (status, id),
            )
        if cur.rowcount == 0:
            log.info("resolve_learning: no pending learning with id=%r (already resolved or absent)", id)

    def park_approval(
        self, turn_id: str, state_blob: bytes, now: datetime, ttl_seconds: int = DEFAULT_APPROVAL_TTL_S
    ) -> None:
        """Stash a paused RISKY turn's resumable state (Story 9.3) keyed by `turn_id`, with an
        `expires_at` = now + ttl. One commit (single writer, AD-5). `INSERT OR REPLACE` so a
        re-parked turn id overwrites cleanly."""
        created = now.isoformat()
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_approvals (turn_id, created_at, expires_at, state_blob) "
                "VALUES (?, ?, ?, ?)",
                (turn_id, created, expires, state_blob),
            )

    def take_approval(self, turn_id: str, now: datetime) -> bytes | None:
        """Consume a parked approval (Story 9.3): return its `state_blob` and DELETE the row,
        atomically in one commit — but ONLY if it hasn't expired. An absent OR expired row
        returns None (the pending call never executes, AC4); an expired one is deleted+logged.
        ISO-8601 UTC strings compare lexicographically, so the `expires_at > now` test is a
        plain string comparison."""
        now_s = now.isoformat()
        with self._conn:
            row = self._conn.execute(
                "SELECT expires_at, state_blob FROM pending_approvals WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                return None
            # Decide expiry INSIDE the transaction, before the DELETE, so the read+expiry+delete
            # are one atomic step. Either way the row is consumed (an expired one is dropped).
            expired = row["expires_at"] <= now_s
            self._conn.execute("DELETE FROM pending_approvals WHERE turn_id = ?", (turn_id,))
        if expired:
            log.info("take_approval: turn %s expired (never executing)", turn_id)
            return None
        return row["state_blob"]

    def prune_expired_approvals(self, now: datetime) -> None:
        """Best-effort housekeeping: drop any approvals past their expiry (Story 9.3)."""
        with self._conn:
            self._conn.execute("DELETE FROM pending_approvals WHERE expires_at <= ?", (now.isoformat(),))

    def park_promotion(
        self, turn_id: str, tool_name: str, now: datetime, ttl_seconds: int = DEFAULT_APPROVAL_TTL_S
    ) -> None:
        """Stash a passed self-coded tool awaiting the owner's Approve tap (Story 9.4), keyed by
        `turn_id`, with `expires_at` = now + ttl. One commit (single writer, AD-5). `INSERT OR
        REPLACE` so a re-parked turn id overwrites cleanly. Parallel to `park_approval` but holds
        only the `tool_name` (the staged files are on disk)."""
        created = now.isoformat()
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_promotions (turn_id, created_at, expires_at, tool_name) "
                "VALUES (?, ?, ?, ?)",
                (turn_id, created, expires, tool_name),
            )

    def take_promotion(self, turn_id: str, now: datetime) -> str | None:
        """Consume a parked promotion (Story 9.4): return its `tool_name` and DELETE the row,
        atomically in one commit — but ONLY if it hasn't expired. An absent OR expired row returns
        None (the tool is never promoted, AC3); an expired one is deleted+logged. Mirrors
        `take_approval`'s read+expiry+delete-in-one-transaction discipline."""
        now_s = now.isoformat()
        with self._conn:
            row = self._conn.execute(
                "SELECT expires_at, tool_name FROM pending_promotions WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                return None
            expired = row["expires_at"] <= now_s
            self._conn.execute("DELETE FROM pending_promotions WHERE turn_id = ?", (turn_id,))
        if expired:
            log.info("take_promotion: turn %s expired (never promoting)", turn_id)
            return None
        return row["tool_name"]

    def prune_expired_promotions(self, now: datetime) -> list[str]:
        """Drop any promotions past their expiry (Story 9.4) and RETURN their `tool_name`s so the
        caller can discard the staged files on disk (Story 9.5 review — deleting the DB row alone
        leaks the staged `<stem>.py`/`test_<stem>.py` pair, since the files live in tools-staging/).
        sqlite-only here (no filesystem — that's core/selfcode's job); core/runtime does the discard."""
        now_s = now.isoformat()
        with self._conn:
            rows = self._conn.execute(
                "SELECT tool_name FROM pending_promotions WHERE expires_at <= ?", (now_s,)
            ).fetchall()
            self._conn.execute("DELETE FROM pending_promotions WHERE expires_at <= ?", (now_s,))
        return [r["tool_name"] for r in rows]

    def record_tool_failure(self, name: str, now: datetime) -> int:
        """Record a self-coded tool failure (Story 9.5, AD-8) — single writer, one commit, ONE
        atomic UPSERT (mirrors `capture_learning`): a first failure inserts `strikes=1`, a repeat
        increments. Returns the new strike count so the caller can quarantine at the threshold."""
        ts = now.isoformat()
        with self._conn:  # UPSERT then read in ONE transaction (no RETURNING — portable to older sqlite)
            self._conn.execute(
                "INSERT INTO tool_health (name, strikes, last_seen) VALUES (?, 1, ?) "
                "ON CONFLICT(name) DO UPDATE SET strikes = strikes + 1, last_seen = excluded.last_seen",
                (name, ts),
            )
            row = self._conn.execute("SELECT strikes FROM tool_health WHERE name = ?", (name,)).fetchone()
        return row["strikes"]

    def tool_strikes(self, name: str) -> int:
        """The current strike count for `name` (0 if absent) — a CORE read for tests/observability."""
        row = self._conn.execute("SELECT strikes FROM tool_health WHERE name = ?", (name,)).fetchone()
        return row["strikes"] if row is not None else 0

    def recent(self, n: int = 20) -> list[sqlite3.Row]:
        return _recent(self._conn, n)

    def search(self, query: str, n: int = 20) -> list[sqlite3.Row]:
        return _search(self._conn, query, n)

    def close(self) -> None:
        self._conn.close()
