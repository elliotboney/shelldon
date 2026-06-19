"""Story 4.1 — the conversation-history store.

Covers the sqlite substrate (WAL + FTS5, ordered+timestamped, batched per-turn
commit — AD-6), the read-only reader seam workers use (write-denied at the
connection — AD-5), the non-breaking multi-user schema shape (AD-13), and core
recording each completed/degraded turn.

Every test uses a `tmp_path` db — never real `$HOME`.
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from conftest import DummySpawner, await_true
from shelldon.core.history import HistoryStore, open_readonly
from shelldon.core.runtime import DEGRADE_TEXT, Core

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


# --- AC1: ordered, timestamped, WAL, FTS5, batched ---


def test_records_turn_ordered_and_timestamped(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.record_turn("t1", "hello pet", "hi owner", NOW)
    rows = s.recent(10)
    assert [r["role"] for r in rows] == ["owner", "pet"]  # owner before pet
    assert rows[0]["content"] == "hello pet"
    assert rows[1]["content"] == "hi owner"
    assert rows[0]["ts"] == NOW.isoformat()
    s.close()


def test_wal_mode_enabled(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    s.close()


def test_fts5_keyword_recall(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.record_turn("t1", "tell me about pumpkins", "pumpkins are great", NOW)
    s.record_turn("t2", "what about cats", "meow", NOW)
    hits = s.search("pumpkins", 10)
    contents = [r["content"].lower() for r in hits]
    assert any("pumpkin" in c for c in contents)
    assert all("meow" not in c for c in contents)  # unrelated turn not matched
    s.close()


def test_one_commit_per_turn(tmp_path):
    """Both rows of a turn land together (single transaction)."""
    s = HistoryStore.open(tmp_path / "h.db")
    s.record_turn("t1", "a", "b", NOW)
    assert s._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    s.close()


def test_recent_is_chronological_across_turns(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.record_turn("t1", "first", "r1", NOW)
    s.record_turn("t2", "second", "r2", NOW)
    rows = s.recent(10)
    assert [r["content"] for r in rows] == ["first", "r1", "second", "r2"]
    # recent(n) caps and keeps the newest n in order
    assert [r["content"] for r in s.recent(2)] == ["second", "r2"]
    s.close()


def test_missing_fts5_raises_clear_error(tmp_path, monkeypatch):
    import shelldon.core.history as history

    def boom(conn):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(history, "_apply_schema", boom)
    with pytest.raises(RuntimeError, match="FTS5"):
        HistoryStore.open(tmp_path / "h.db")


# --- AC2: read-only reader ---


def test_readonly_reads_but_cannot_write(tmp_path):
    db = tmp_path / "h.db"
    s = HistoryStore.open(db)
    s.record_turn("t1", "question", "answer", NOW)
    s.close()

    r = open_readonly(db)
    assert len(r.recent(10)) == 2
    assert any("question" in row["content"] for row in r.search("question", 10))
    with pytest.raises(sqlite3.OperationalError):
        r._conn.execute("INSERT INTO messages(role, content, ts) VALUES ('owner', 'x', 'y')")
    r.close()


def test_readonly_reader_has_no_write_method(tmp_path):
    db = tmp_path / "h.db"
    HistoryStore.open(db).close()
    r = open_readonly(db)
    assert not hasattr(r, "record_turn")  # read-only seam exposes no writer
    r.close()


# --- AC3: non-breaking multi-user shape ---


def test_schema_allows_nonbreaking_user_id_add(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.record_turn("t1", "q", "a", NOW)
    # A later story adds chat_id/user_id as a nullable column — non-destructive.
    s._conn.execute("ALTER TABLE messages ADD COLUMN user_id TEXT")
    rows = s._conn.execute("SELECT user_id FROM messages").fetchall()
    assert all(row[0] is None for row in rows)  # existing rows read back NULL
    s.close()


# --- Story 6.1: the learnings table (capture on the hot path, AD-6) ---

LATER = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _learnings(s):
    return s._conn.execute(
        "SELECT pattern_key, observation, recurrence_count, status, first_seen, last_seen "
        "FROM learnings ORDER BY id"
    ).fetchall()


def test_capture_learning_inserts_a_pending_row(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("owner prefers BigQuery", "prefers-bigquery", NOW)
    rows = _learnings(s)
    assert len(rows) == 1
    r = rows[0]
    assert r["pattern_key"] == "prefers-bigquery"
    assert r["observation"] == "owner prefers BigQuery"
    assert r["recurrence_count"] == 1
    assert r["status"] == "pending"
    assert r["first_seen"] == NOW.isoformat() == r["last_seen"]
    s.close()


def test_capture_learning_dedups_by_pattern_key_and_increments(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("seen it", "k", NOW)
    s.capture_learning("seen it again", "k", LATER)
    rows = _learnings(s)
    assert len(rows) == 1                              # deduped, not duplicated
    assert rows[0]["recurrence_count"] == 2
    assert rows[0]["last_seen"] == LATER.isoformat()   # refreshed
    assert rows[0]["first_seen"] == NOW.isoformat()    # original first_seen kept
    s.close()


def test_recurrence_resets_status_to_pending(tmp_path):
    """AD-6: a re-captured learning refreshes to status='pending' — so one the dream already
    promoted/pruned but that keeps recurring re-enters the queue."""
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("obs", "k", NOW)
    with s._conn:
        s._conn.execute("UPDATE learnings SET status='promoted' WHERE pattern_key='k'")
    s.capture_learning("obs", "k", LATER)
    row = _learnings(s)[0]
    assert row["status"] == "pending"        # reset
    assert row["recurrence_count"] == 2
    s.close()


def test_none_pattern_key_always_inserts(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("same text", None, NOW)
    s.capture_learning("same text", None, NOW)
    assert len(_learnings(s)) == 2  # no dedup key -> two rows
    s.close()


def test_blank_pattern_key_normalizes_to_none(tmp_path):
    """A blank/whitespace pattern_key must NOT become a dedup bucket — treat it as None."""
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("x", "  ", NOW)
    s.capture_learning("y", "", NOW)
    rows = _learnings(s)
    assert len(rows) == 2
    assert all(r["pattern_key"] is None for r in rows)
    s.close()


def test_empty_observation_is_skipped_not_written(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("   \n ", "k", NOW)  # whitespace-only
    s.capture_learning("", None, NOW)
    assert _learnings(s) == []
    s.close()


def test_dedup_is_db_enforced_atomic_upsert(tmp_path):
    """6.1-review TOCTOU fix: dedup is a UNIQUE partial index + atomic UPSERT, not a
    SELECT-then-UPDATE. A raw duplicate INSERT of the same non-NULL pattern_key is rejected
    at the DB layer — so a second writer (6.2's dream) can't create a duplicate row. NULL
    keys stay exempt (multiple allowed)."""
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("o", "k", NOW)
    with pytest.raises(sqlite3.IntegrityError):  # the unique partial index forbids a second 'k'
        with s._conn:
            s._conn.execute(
                "INSERT INTO learnings (pattern_key, observation, first_seen, last_seen) "
                "VALUES ('k', 'dup', ?, ?)",
                (NOW.isoformat(), NOW.isoformat()),
            )
    # NULL keys are exempt from the uniqueness — two raw NULL inserts both succeed.
    with s._conn:
        s._conn.execute(
            "INSERT INTO learnings (pattern_key, observation, first_seen, last_seen) "
            "VALUES (NULL, 'a', ?, ?)", (NOW.isoformat(), NOW.isoformat()))
        s._conn.execute(
            "INSERT INTO learnings (pattern_key, observation, first_seen, last_seen) "
            "VALUES (NULL, 'b', ?, ?)", (NOW.isoformat(), NOW.isoformat()))
    assert s._conn.execute("SELECT COUNT(*) FROM learnings WHERE pattern_key IS NULL").fetchone()[0] == 2
    s.close()


def test_status_check_constraint_rejects_unknown_status(tmp_path):
    s = HistoryStore.open(tmp_path / "h.db")
    s.capture_learning("o", "k", NOW)
    with pytest.raises(sqlite3.IntegrityError):
        with s._conn:
            s._conn.execute("UPDATE learnings SET status='bogus' WHERE pattern_key='k'")
    s.close()


def test_readonly_reader_has_no_learnings_write(tmp_path):
    """The worker's read-only handle exposes no learnings writer (6.2 adds the read path)."""
    db = tmp_path / "h.db"
    HistoryStore.open(db).close()
    r = open_readonly(db)
    assert not hasattr(r, "capture_learning")
    r.close()


# --- AC1 (core integration): a completed turn is recorded ---


async def test_core_records_completed_turn(sock_path, tmp_path):
    from test_end_to_end_turn import OkProvider, Spawns, build_harness

    h = await build_harness(sock_path, provider=OkProvider(), spawns=Spawns())
    try:
        h.source.feed("hello pet")
        await await_true(lambda: h.outbound == ["reply to: hello pet"])
        await await_true(lambda: len(h.core.history.recent(10)) >= 2)
        rows = h.core.history.recent(10)
        assert rows[-2]["role"] == "owner" and rows[-2]["content"] == "hello pet"
        assert rows[-1]["role"] == "pet" and rows[-1]["content"] == "reply to: hello pet"
    finally:
        await h.teardown()


async def test_core_records_degraded_turn(sock_path, tmp_path):
    from test_end_to_end_turn import AlwaysTransientProvider, Spawns, build_harness

    h = await build_harness(sock_path, provider=AlwaysTransientProvider(), spawns=Spawns())
    try:
        h.source.feed("you there?")
        await await_true(lambda: h.outbound == [DEGRADE_TEXT])
        await await_true(lambda: len(h.core.history.recent(10)) >= 2)
        rows = h.core.history.recent(10)
        assert rows[-2]["content"] == "you there?"
        assert rows[-1]["content"] == DEGRADE_TEXT  # the pet's actual reply that turn
    finally:
        await h.teardown()


def test_history_write_failure_does_not_crash_the_turn(sock_path, tmp_path):
    """History is best-effort: a sqlite failure after the reply is delivered must be
    logged, not raised (it would otherwise kill the core turn loop). (Review: Medium)"""
    core = Core(sock_path, DummySpawner(), checkpoint_path=tmp_path / "s.json")
    core._current_prompt = "hello"
    core._current_turn_id = "t1"

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    core.history.record_turn = boom
    core._record_turn("a reply")  # must NOT raise
    core.history.close()


def test_spawn_failure_records_nothing(sock_path, tmp_path):
    """A turn that never ran (spawn failed) leaves no history row."""
    from shelldon.worker.forkserver import WorkerBusyError

    class _FailingSpawner:
        async def ready(self):
            pass

        async def spawn_turn(self, turn_id, prompt):
            raise WorkerBusyError("nope")

        async def reap_current(self):  # pragma: no cover
            pass

    core = Core(sock_path, _FailingSpawner(), checkpoint_path=tmp_path / "s.json")
    import asyncio

    prompt = core.arbiter.submit("hello")
    asyncio.run(core._start_turn(prompt))
    assert core.history.recent(10) == []
    core.history.close()
