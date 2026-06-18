"""Story 4.4 — worker-side prompt assembly: pure compose, safe recall, fail-soft gather.

Cross-platform, no network. The pure `assemble_prompt` is tested for the binding AD-6
order + section omission + de-dup; `_fts_query` for punctuation safety; `gather_context`
for resilience (missing/locked history, FTS-hostile messages, missing memory).
"""

from datetime import UTC, datetime

from shelldon.core.history import HistoryStore
from shelldon.core.memory import CuratedMemory
from shelldon.contracts import RewriteAbout
from shelldon.worker.prompt import (
    SYSTEM_INSTRUCTION,
    _fts_query,
    assemble_prompt,
    gather_context,
)


# --- pure assemble_prompt ---


def test_order_is_directive_about_recent_recall_current():
    out = assemble_prompt(
        "what now?",
        directive="obey the owner",
        about="i am shelldon",
        recent=[("owner", "earlier hi"), ("pet", "earlier hello")],
        recall=[("owner", "long ago fact")],
    )
    # Each section appears, in the binding AD-6 order.
    i_dir = out.index("obey the owner")
    i_about = out.index("i am shelldon")
    i_recent = out.index("earlier hi")
    i_recall = out.index("long ago fact")
    i_now = out.index("what now?")
    assert i_dir < i_about < i_recent < i_recall < i_now
    assert SYSTEM_INSTRUCTION in out  # system instruction present
    assert out.rstrip().endswith("what now?")  # current message is last


def test_missing_sections_are_omitted_not_blank():
    out = assemble_prompt("hi", directive=None, about=None, recent=(), recall=())
    assert "# Owner directive" not in out
    assert "# About you" not in out
    assert "# Recent conversation" not in out
    assert "# Things you remember" not in out
    assert out.rstrip().endswith("hi")


def test_blank_directive_about_omitted():
    out = assemble_prompt("hi", directive="   ", about="\n")
    assert "# Owner directive" not in out and "# About you" not in out


# --- _fts_query safety ---


def test_fts_query_quotes_terms_and_ors():
    q = _fts_query("favorite database")
    assert q == '"favorite" OR "database"'


def test_fts_query_defuses_punctuation_and_operators():
    # Raw FTS5 operators/punctuation must not survive into the query unquoted.
    q = _fts_query('what\'s up?? (re: NEAR/AND $$$)')
    assert q is not None
    assert "(" not in q and "$" not in q and "?" not in q
    # operators only ever appear inside quotes (as literal terms), never bare
    assert " AND " not in q and "NEAR" not in q.replace('"NEAR"', "")


def test_fts_query_none_when_no_words():
    assert _fts_query("?? !! ...") is None
    assert _fts_query("") is None


# --- gather_context (I/O + fail-soft) ---


def _seed_history(path, pairs):
    store = HistoryStore.open(path)
    for i, (owner, pet) in enumerate(pairs):
        store.record_turn(f"t{i}", owner, pet, datetime.now(UTC))
    store.close()


def test_gather_reads_recent_and_recall(tmp_path):
    hist = tmp_path / "history.db"
    _seed_history(hist, [("my favorite database is BigQuery", "noted"), ("hello", "hi")])

    ctx = gather_context(tmp_path / "memory", hist, "what is my favorite database?", recent_n=10, recall_k=5)

    recent_blob = " ".join(c for _, c in ctx["recent"])
    assert "BigQuery" in recent_blob  # recent window carries the earlier fact
    assert ctx["directive"] is None and ctx["about"] is None  # no memory seeded


def test_gather_recall_surfaces_beyond_recent_window(tmp_path):
    hist = tmp_path / "history.db"
    # One distinctive old turn, then fillers that push it out of a tiny recent window.
    pairs = [("the launch code is orange-walrus", "ok")] + [(f"filler {i}", "k") for i in range(6)]
    _seed_history(hist, pairs)

    ctx = gather_context(tmp_path / "memory", hist, "what is the launch code?", recent_n=1, recall_k=5)

    recent_blob = " ".join(c for _, c in ctx["recent"])
    recall_blob = " ".join(c for _, c in ctx["recall"])
    assert "orange-walrus" not in recent_blob  # outside the tiny recent window
    assert "orange-walrus" in recall_blob  # but FTS5 recall surfaced it


def test_gather_dedupes_recall_against_recent(tmp_path):
    hist = tmp_path / "history.db"
    _seed_history(hist, [("zebra-fact one", "ok")])

    ctx = gather_context(tmp_path / "memory", hist, "tell me the zebra-fact", recent_n=10, recall_k=5)

    # The only matching row is already in the (large) recent window → recall de-dupes it out.
    assert any("zebra-fact" in c for _, c in ctx["recent"])
    assert ctx["recall"] == []


def test_gather_missing_history_degrades(tmp_path):
    # No db file exists → opening read-only fails → degrade, no raise, empty windows.
    ctx = gather_context(tmp_path / "memory", tmp_path / "nope.db", "anything", recent_n=5, recall_k=5)
    assert ctx["recent"] == [] and ctx["recall"] == []


def test_gather_fts_hostile_message_no_crash(tmp_path):
    hist = tmp_path / "history.db"
    _seed_history(hist, [("a normal message", "ok")])
    # Unbalanced quotes / bare operators would crash a naive MATCH — must not here.
    ctx = gather_context(tmp_path / "memory", hist, '"unbalanced (quote AND OR *', recent_n=5, recall_k=5)
    assert isinstance(ctx["recall"], list)  # no exception escaped


def test_gather_corrupt_about_degrades_not_raises(tmp_path):
    # A non-UTF-8 about.md makes read_text() raise UnicodeDecodeError (a ValueError, not
    # OSError) — gather must catch it and degrade (AC3), not raise into the turn.
    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True)
    (mem_root / "about.md").write_bytes(b"\xff\xfe not utf-8")
    (mem_root / "DIRECTIVE.md").write_text("be kind")

    ctx = gather_context(mem_root, tmp_path / "history.db", "hi", recent_n=5, recall_k=5)
    assert ctx["about"] is None  # corrupt file degraded to None
    assert ctx["directive"] == "be kind"  # the readable file still came through


def test_gather_reads_seeded_memory(tmp_path):
    mem_root = tmp_path / "memory"
    CuratedMemory(mem_root).apply_memory_op(RewriteAbout(content="i am shelldon, owned by Elliot"))
    (mem_root / "DIRECTIVE.md").write_text("be kind")

    ctx = gather_context(mem_root, tmp_path / "history.db", "hi", recent_n=5, recall_k=5)
    assert "Elliot" in ctx["about"]
    assert ctx["directive"] == "be kind"
