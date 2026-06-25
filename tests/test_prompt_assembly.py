"""Story 4.4 — worker-side prompt assembly: pure compose, safe recall, fail-soft gather.

Cross-platform, no network. The pure `assemble_prompt` is tested for the binding AD-6
order + section omission + de-dup; `_fts_query` for punctuation safety; `gather_context`
for resilience (missing/locked history, FTS-hostile messages, missing memory).
"""

from datetime import UTC, datetime

from shelldon.core.history import HistoryStore
from shelldon.core.memory import CuratedMemory
from shelldon.contracts import RewriteAbout, RewriteSummary, RewriteUser
from shelldon.worker.prompt import (
    _fts_query,
    assemble_prompt,
    build_prompt,
    gather_context,
    seed_instructions,
)


# --- pure assemble_prompt ---


def test_order_is_directive_about_recent_recall_current():
    out = assemble_prompt(
        "what now?",
        system="SYS-INSTRUCTION",
        directive="obey the owner",
        about="i am shelldon",
        recent=[("owner", "earlier hi"), ("pet", "earlier hello")],
        recall=[("owner", "long ago fact")],
    )
    # Each section appears, in the binding AD-6 order.
    i_sys = out.index("SYS-INSTRUCTION")
    i_dir = out.index("obey the owner")
    i_about = out.index("i am shelldon")
    i_recent = out.index("earlier hi")
    i_recall = out.index("long ago fact")
    i_now = out.index("what now?")
    assert i_sys < i_dir < i_about < i_recent < i_recall < i_now
    assert out.rstrip().endswith("what now?")  # current message is last


def test_persona_order_identity_soul_user_after_directive_before_about():
    """Story 10.1 — the persona files inject right after the authoritative directive and
    before about/knowledge/recall, so persona shapes every reply (binding AD-6 order)."""
    out = assemble_prompt(
        "now?",
        system="SYS",
        directive="obey the owner",
        identity="i am shelldon on a Pi",
        soul="curious and warm",
        user="owner is Elliot",
        about="self summary",
    )
    i_dir = out.index("obey the owner")
    i_identity = out.index("i am shelldon on a Pi")
    i_soul = out.index("curious and warm")
    i_user = out.index("owner is Elliot")
    i_about = out.index("self summary")
    assert i_dir < i_identity < i_soul < i_user < i_about
    assert "# Your identity" in out and "# Your soul" in out and "# Your owner" in out


def test_empty_persona_sections_omitted():
    """Day-one parity hinge — empty/blank persona files contribute NO section (no empty headers)."""
    out = assemble_prompt("hi", system="SYS", identity="", soul="   ", user=None)
    assert "# Your identity" not in out
    assert "# Your soul" not in out
    assert "# Your owner" not in out


def test_golden_day_one_system_seed_plus_onboarding(tmp_path):
    """AC7 — the prompt assembled from SEED files (BOT_INSTRUCTIONS verbatim + empty
    SOUL/IDENTITY/USER) carries the verbatim system seed, the empty persona omitted, plus
    (Story 10.4) the first-run onboarding directive — since USER.md ships blank, onboarding is
    active on a freshly-seeded root.

    `gather_context` over a freshly-seeded root yields `system`==BOT_INSTRUCTIONS, empty persona
    (omitted), and a non-None `bootstrap`, so the assembled prompt ==
    `assemble_prompt(msg, system=seed_text, bootstrap=bootstrap_seed)`."""
    msg = "hello shelldon"
    ctx = gather_context(tmp_path / "memory", tmp_path / "h.db", msg)
    assembled = assemble_prompt(msg, **ctx)
    # System seed + onboarding directive (10.4: blank USER -> onboarding) + the owner message.
    expected = assemble_prompt(msg, system=seed_instructions(), bootstrap=ctx["bootstrap"])
    assert assembled == expected
    # The system block IS the verbatim repo seed, and onboarding is present (USER blank).
    assert ctx["system"] == seed_instructions()
    assert ctx["bootstrap"] is not None
    assert assembled.startswith(seed_instructions())
    assert "# First-run onboarding" in assembled
    assert assembled.rstrip().endswith(msg)


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


# --- Story 6.2: the dream's running summary is injected (after about, before recent) ---


def test_summary_injected_after_about_before_recent():
    out = assemble_prompt(
        "now?",
        about="i am shelldon",
        summary="owner migrating to BigQuery",
        recent=[("owner", "earlier hi")],
    )
    i_about = out.index("i am shelldon")
    i_summary = out.index("owner migrating to BigQuery")
    i_recent = out.index("earlier hi")
    assert i_about < i_summary < i_recent
    assert "# Conversation so far" in out


def test_missing_summary_omitted_not_blank():
    out = assemble_prompt("hi", about="me", summary=None)
    assert "# Conversation so far" not in out
    out2 = assemble_prompt("hi", summary="   ")  # blank -> omitted
    assert "# Conversation so far" not in out2


# --- Epic 6 retro action #2: facts/ + people/ surfaced into the prompt ---


def test_knowledge_injected_after_about_before_summary():
    out = assemble_prompt(
        "now?",
        about="i am shelldon",
        knowledge=[("favorite-db", "BigQuery"), ("Alex", "owner friend")],
        summary="a summary",
    )
    i_about = out.index("i am shelldon")
    i_know = out.index("# What you know")
    i_summary = out.index("a summary")
    assert i_about < i_know < i_summary
    assert "favorite-db: BigQuery" in out and "Alex: owner friend" in out


def test_empty_knowledge_omitted():
    out = assemble_prompt("hi", about="me", knowledge=())
    assert "# What you know" not in out


def test_gather_context_surfaces_facts_and_people(tmp_path):
    """gather_context reads facts/ + people/ so a promoted fact reaches the prompt (closes the
    6.2-discovered gap — facts/ was durable but never injected)."""
    from shelldon.core.memory import CuratedMemory
    from shelldon.contracts import Remember

    mem = CuratedMemory(tmp_path / "memory")
    mem.apply_memory_op(Remember(collection="facts", name="dog", content="named Pixel"))
    mem.apply_memory_op(Remember(collection="people", name="Alex", content="owner friend"))
    ctx = gather_context(memory_root=tmp_path / "memory", history_path=tmp_path / "h.db", owner_message="hi")
    flat = dict(ctx["knowledge"])
    # names become casefolded filename stems (_safe_filename): "Alex" -> "alex"
    assert flat["dog"] == "named Pixel" and flat["alex"] == "owner friend"
    # missing tree -> empty, no raise
    assert gather_context(memory_root=tmp_path / "none", history_path=tmp_path / "h2.db", owner_message="hi")["knowledge"] == []


def test_gather_context_surfaces_preferences_and_capabilities(tmp_path):
    """gather_context surfaces the whole collection set — a `preferences`/`capabilities`
    the model curated reaches later prompts the same way facts/people do."""
    from shelldon.core.memory import CuratedMemory
    from shelldon.contracts import Remember

    mem = CuratedMemory(tmp_path / "memory")
    mem.apply_memory_op(Remember(collection="preferences", name="theme", content="dark mode"))
    mem.apply_memory_op(Remember(collection="capabilities", name="coding", content="can write code"))
    ctx = gather_context(memory_root=tmp_path / "memory", history_path=tmp_path / "h.db", owner_message="hi")
    flat = dict(ctx["knowledge"])
    assert flat["theme"] == "dark mode" and flat["coding"] == "can write code"


def test_gather_context_includes_summary(tmp_path):
    """gather_context reads summary.md and returns it for assembly; missing -> None (no raise)."""
    from shelldon.core.memory import CuratedMemory

    CuratedMemory(tmp_path / "memory").apply_memory_op(RewriteSummary(content="a running summary"))
    ctx = gather_context(memory_root=tmp_path / "memory", history_path=tmp_path / "h.db", owner_message="hi")
    assert ctx["summary"] == "a running summary"
    # no summary file -> None, never raises
    ctx2 = gather_context(memory_root=tmp_path / "empty", history_path=tmp_path / "h2.db", owner_message="hi")
    assert ctx2["summary"] is None


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


def test_gather_corrupt_persona_degrades_only_its_section(tmp_path):
    """Story 10.1 AC6 — a corrupt (non-UTF-8) SOUL.md degrades ONLY soul; the system
    instruction + other persona sections still come through (independent fail-soft)."""
    mem_root = tmp_path / "memory"
    CuratedMemory(mem_root)  # seed the persona files first
    (mem_root / "SOUL.md").write_bytes(b"\xff\xfe not utf-8")  # corrupt one file

    ctx = gather_context(mem_root, tmp_path / "history.db", "hi", recent_n=5, recall_k=5)
    assert ctx["soul"] is None  # corrupt section degraded
    assert ctx["system"] and "You are shelldon" in ctx["system"]  # system survived
    assert ctx["identity"] == "" and ctx["user"] == ""  # sibling persona reads survived


def test_gather_seeds_and_reads_persona_system(tmp_path):
    """Story 10.1 — gather over a fresh root seeds + reads BOT_INSTRUCTIONS into `system`."""
    ctx = gather_context(tmp_path / "memory", tmp_path / "h.db", "hi")
    assert ctx["system"] == seed_instructions()
    assert ctx["soul"] == "" and ctx["identity"] == "" and ctx["user"] == ""


def test_gather_reads_seeded_memory(tmp_path):
    mem_root = tmp_path / "memory"
    CuratedMemory(mem_root).apply_memory_op(RewriteAbout(content="i am shelldon, owned by Elliot"))
    (mem_root / "DIRECTIVE.md").write_text("be kind")

    ctx = gather_context(mem_root, tmp_path / "history.db", "hi", recent_n=5, recall_k=5)
    assert "Elliot" in ctx["about"]
    assert ctx["directive"] == "be kind"


# --- Story 10.4: first-run onboarding (BOOTSTRAP.md injected while USER.md is blank) ---


def test_assemble_onboarding_after_system_before_directive():
    """AC4 — the pure assembler places the onboarding section right after the system block and
    BEFORE directive/persona, with the owner message still last."""
    out = assemble_prompt(
        "hi",
        system="SYS",
        bootstrap="get to know your owner",
        directive="obey the owner",
        identity="i am shelldon",
    )
    i_sys = out.index("SYS")
    i_boot = out.index("get to know your owner")
    i_dir = out.index("obey the owner")
    i_now = out.index("hi")
    assert i_sys < i_boot < i_dir < i_now
    assert "# First-run onboarding" in out
    assert out.rstrip().endswith("hi")


def test_assemble_blank_bootstrap_omitted():
    """AC4 — a None/blank bootstrap is omitted entirely (no empty header)."""
    assert "# First-run onboarding" not in assemble_prompt("hi", system="SYS", bootstrap=None)
    assert "# First-run onboarding" not in assemble_prompt("hi", system="SYS", bootstrap="   ")


def test_onboarding_active_while_user_blank(tmp_path):
    """AC2 — a fresh seeded root (USER.md blank) -> gather returns a non-None bootstrap and
    build_prompt injects the onboarding section, positioned after system and before the owner msg."""
    root = tmp_path / "memory"
    ctx = gather_context(root, tmp_path / "h.db", "hi")
    assert ctx["bootstrap"] is not None and "rewrite_user" in ctx["bootstrap"]

    out = build_prompt("hi there", memory_root=root, history_path=tmp_path / "h.db")
    assert "# First-run onboarding" in out
    i_sys = out.index(seed_instructions())
    i_boot = out.index("# First-run onboarding")
    i_now = out.index("hi there")
    assert i_sys < i_boot < i_now


def test_onboarding_stops_once_user_filled(tmp_path):
    """AC3 — once core applies a rewrite_user (USER.md non-blank, the monotonic sentinel) gather
    returns bootstrap=None, build_prompt OMITS onboarding, and the populated `# Your owner` appears."""
    root = tmp_path / "memory"
    CuratedMemory(root).apply_memory_op(RewriteUser(content="owner is Elliot, likes terse replies"))

    ctx = gather_context(root, tmp_path / "h.db", "hi")
    assert ctx["bootstrap"] is None
    assert ctx["user"] == "owner is Elliot, likes terse replies"

    out = build_prompt("hi", memory_root=root, history_path=tmp_path / "h.db")
    assert "# First-run onboarding" not in out
    assert "# Your owner" in out and "owner is Elliot" in out


def test_onboarding_fail_soft_when_bootstrap_corrupt(tmp_path):
    """AC5 — USER still blank but BOOTSTRAP.md is corrupt (non-UTF-8) -> read fails soft to None, the
    onboarding section is omitted, the turn proceeds as a normal turn, never raises. (Corruption, not
    deletion, because gather_context re-seeds an absent file on construction — a present-but-corrupt
    file is never overwritten, so it exercises the worker's fail-soft read.)"""
    root = tmp_path / "memory"
    CuratedMemory(root)  # seed first
    (root / "BOOTSTRAP.md").write_bytes(b"\xff\xfe bad bytes")  # present but undecodable

    ctx = gather_context(root, tmp_path / "h.db", "hi")
    assert ctx["bootstrap"] is None  # corrupt -> None, not a raise
    out = build_prompt("hello", memory_root=root, history_path=tmp_path / "h.db")
    assert "# First-run onboarding" not in out
    assert out.rstrip().endswith("hello")
