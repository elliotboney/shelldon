"""Story 4.2 — the curated markdown memory tree + the core-only apply path.

Asserts: closed memory-op schemas (typo/unknown rejected), atomic writes that survive
a crash before os.replace, about.md persistence, the read-only DIRECTIVE.md accessor +
the disjoint-writer guarantee (no memory-op can write DIRECTIVE.md), and people/facts
placement with path-traversal rejection. Every test uses an injected tmp_path root —
never real $HOME.
"""

import os

import msgspec
import pytest

from shelldon.contracts import LogEpisode, MemoryOp, Remember, RewriteAbout, RewriteSummary
from shelldon.core.memory import (
    _PERSONA_SEED_FILES,
    _PROMPT_TEMPLATE_SEED_FILES,
    CuratedMemory,
)


def _mem(tmp_path):
    return CuratedMemory(tmp_path / "memory")


# --- Story 10.1: persona files seed copy-if-absent + read accessors ---

_PERSONA_FILES = ("BOT_INSTRUCTIONS.md", "SOUL.md", "IDENTITY.md", "USER.md")


def test_seed_persona_on_absent_creates_all_files(tmp_path):
    """An empty root → constructing CuratedMemory copies every persona seed in from the shipped
    templates. BOT_INSTRUCTIONS carries the system text; SOUL/IDENTITY ship with STARTER content (a
    bot is born with a soul + sense of self, then evolves them); only USER ships blank (the
    onboarding trigger — its content is the per-owner profile, learned via the interview)."""
    root = tmp_path / "memory"
    assert not root.exists()
    mem = CuratedMemory(root)
    for name in _PERSONA_FILES:
        assert (root / name).is_file(), f"{name} not seeded"
    assert "You are shelldon" in mem.read_instructions()
    assert "Soul" in mem.read_soul() and mem.read_soul().strip()  # starter personality
    assert "shelldon" in mem.read_identity()  # starter self-facts
    assert mem.read_user() == ""  # only USER ships blank (onboarding fills it)


def test_seed_persona_skips_present_files(tmp_path):
    """Seeding only fills ABSENT files — a present file (owner/bot hand-edit) is never overwritten."""
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    (root / "BOT_INSTRUCTIONS.md").write_text("MY OWN INSTRUCTIONS")
    mem = CuratedMemory(root)
    assert mem.read_instructions() == "MY OWN INSTRUCTIONS"  # untouched
    # the absent ones still get seeded
    assert (root / "SOUL.md").is_file()


def test_seed_persona_is_idempotent_across_constructions(tmp_path):
    """Re-constructing (core boot + per fork worker both build a CuratedMemory) never rewrites a
    present file — second construction is a no-op for content."""
    root = tmp_path / "memory"
    CuratedMemory(root)
    (root / "BOT_INSTRUCTIONS.md").write_text("EDITED")
    CuratedMemory(root)  # second construction
    assert (root / "BOT_INSTRUCTIONS.md").read_text() == "EDITED"


def test_seed_prompt_templates_on_absent_and_skip_present(tmp_path):
    """Story 10.3: HEARTBEAT.md/DREAM.md seed copy-if-absent alongside the persona files, and a
    present one is never overwritten. They are read accessors with no write path (owner-editable)."""
    root = tmp_path / "memory"
    mem = CuratedMemory(root)  # empty root -> both seeded
    assert (root / "HEARTBEAT.md").is_file() and (root / "DREAM.md").is_file()
    assert "{feeling}" in mem.read_heartbeat()  # the seed carries the fill placeholder
    assert "{lines}" in mem.read_dream()
    # a present file (owner hand-edit) is left untouched on re-construction
    (root / "HEARTBEAT.md").write_text("OWNER HEARTBEAT")
    CuratedMemory(root)
    assert mem.read_heartbeat() == "OWNER HEARTBEAT"


def test_read_prompt_template_none_when_absent(tmp_path):
    """An absent HEARTBEAT/DREAM reads None (the is_file guard) -> the builder falls back, never raises."""
    mem = CuratedMemory(tmp_path / "memory")
    (mem.root / "HEARTBEAT.md").unlink()
    (mem.root / "DREAM.md").unlink()
    assert mem.read_heartbeat() is None
    assert mem.read_dream() is None


def test_seed_bootstrap_on_absent_and_skip_present(tmp_path):
    """Story 10.4 (AC1): BOOTSTRAP.md seeds copy-if-absent alongside the other prompt templates,
    carries the warm interview directive, and a present (owner hand-edited) copy is never overwritten."""
    root = tmp_path / "memory"
    mem = CuratedMemory(root)  # empty root -> seeded
    assert (root / "BOOTSTRAP.md").is_file()
    assert "rewrite_user" in mem.read_bootstrap()  # the seed instructs saving the owner profile
    # a present file (owner hand-edit) is left untouched on re-construction
    (root / "BOOTSTRAP.md").write_text("OWNER BOOTSTRAP")
    CuratedMemory(root)
    assert mem.read_bootstrap() == "OWNER BOOTSTRAP"


def test_read_bootstrap_none_when_absent(tmp_path):
    """Story 10.4 (AC5): an absent BOOTSTRAP.md reads None (is_file guard) -> onboarding section is
    omitted, the turn proceeds, never raises. Delete on the SAME instance (construction re-seeds)."""
    mem = CuratedMemory(tmp_path / "memory")
    (mem.root / "BOOTSTRAP.md").unlink()
    assert mem.read_bootstrap() is None


def test_seed_reference_docs_on_absent_and_skip_present(tmp_path):
    """Story 10.5 (AC3/AC4): TOOLS.md/ARCHITECTURE.md seed copy-if-absent alongside the other prompt
    templates, carry real content, and a present (owner hand-edited) copy is never overwritten."""
    root = tmp_path / "memory"
    mem = CuratedMemory(root)  # empty root -> both seeded
    assert (root / "TOOLS.md").is_file() and (root / "ARCHITECTURE.md").is_file()
    assert "get_time" in mem.read_tools()  # the tool surface
    assert "Pi Zero" in mem.read_architecture()  # the hardware answer
    # a present file (owner hand-edit) is left untouched on re-construction
    (root / "TOOLS.md").write_text("OWNER TOOLS")
    CuratedMemory(root)
    assert mem.read_tools() == "OWNER TOOLS"


def test_read_reference_docs_none_when_absent(tmp_path):
    """Story 10.5 (AC3): an absent TOOLS/ARCHITECTURE reads None (is_file guard) -> the lazy-load
    section is omitted, never raises. Delete on the SAME instance (construction re-seeds)."""
    mem = CuratedMemory(tmp_path / "memory")
    (mem.root / "TOOLS.md").unlink()
    (mem.root / "ARCHITECTURE.md").unlink()
    assert mem.read_tools() is None
    assert mem.read_architecture() is None


def test_migration_is_non_destructive_on_populated_root(tmp_path):
    """Story 10.5 (AC4) — Pi migration lands non-destructively: on a populated memory root (an
    owner-written DIRECTIVE.md + existing about.md/facts/), constructing CuratedMemory adds ONLY
    the absent seeds (incl. the new TOOLS/ARCHITECTURE) and leaves every existing file BYTE-for-byte
    untouched. Copy-if-absent never shadows an owner edit."""
    root = tmp_path / "memory"
    (root / "facts").mkdir(parents=True)
    existing = {
        root / "about.md": "i am shelldon, owned by Elliot",
        root / "facts" / "x.md": "the sky is blue",
        root / "DIRECTIVE.md": "be kind and terse",  # hand-written by the owner
    }
    for path, text in existing.items():
        path.write_text(text)

    CuratedMemory(root)  # the "migration" — a fresh construction on the live root

    # every pre-existing file is byte-for-byte untouched
    for path, text in existing.items():
        assert path.read_text() == text, f"{path.name} was modified by migration"
    # and EVERY absent seed (all 9, incl. 10.5's reference docs) was added
    for name in _PERSONA_SEED_FILES + _PROMPT_TEMPLATE_SEED_FILES:
        assert (root / name).is_file(), f"{name} not seeded on migration"


def test_read_persona_accessor_none_when_file_absent(tmp_path):
    """An accessor returns None for an absent file (the is_file guard, mirroring read_about) —
    so a failed/missing seed degrades the section, never raises. Delete after seeding, then read
    on the SAME instance (construction would re-seed)."""
    root = tmp_path / "memory"
    mem = CuratedMemory(root)  # seeds the files
    (root / "SOUL.md").unlink()  # simulate an absent/failed-seed file
    assert mem.read_soul() is None  # accessor tolerates it, no raise


# --- Story 6.2: the running summary (rewrite_summary -> summary.md) ---


def test_rewrite_summary_writes_summary_md(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(RewriteSummary(content="owner is migrating to BigQuery; mood upbeat"))
    assert (tmp_path / "memory" / "summary.md").read_text() == "owner is migrating to BigQuery; mood upbeat"
    assert mem.read_summary() == "owner is migrating to BigQuery; mood upbeat"


def test_rewrite_summary_rejects_empty_content(tmp_path):
    mem = _mem(tmp_path)
    with pytest.raises(ValueError):
        mem.apply_memory_op(RewriteSummary(content="   \n"))
    assert not (tmp_path / "memory" / "summary.md").exists()  # nothing written on reject


def test_read_summary_none_before_written(tmp_path):
    assert _mem(tmp_path).read_summary() is None


# --- facts/ + people/ surfacing (Epic 6 retro action #2: 4.4 extension) ---


def test_read_collection_returns_name_content_sorted(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="facts", name="zebra", content="stripey"))
    mem.apply_memory_op(Remember(collection="facts", name="apple", content="red"))
    assert mem.read_collection("facts") == [("apple", "red"), ("zebra", "stripey")]  # sorted by name


def test_read_collection_missing_dir_is_empty(tmp_path):
    assert _mem(tmp_path).read_collection("facts") == []  # never written -> [], no raise


def test_read_collection_rejects_unknown_collection(tmp_path):
    with pytest.raises(ValueError):
        _mem(tmp_path).read_collection("secrets")  # still closed — vault/DIRECTIVE never readable


def test_apply_remember_to_preferences_and_capabilities(tmp_path):
    """GLM files memories under `preferences` and `capabilities` (observed live on the Pi);
    both are valid collections now, written one-file-per-thing like facts/people."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="preferences", name="theme", content="dark mode"))
    mem.apply_memory_op(Remember(collection="capabilities", name="coding", content="can write code"))
    assert (tmp_path / "memory" / "preferences" / "theme.md").read_text() == "dark mode"
    assert (tmp_path / "memory" / "capabilities" / "coding.md").read_text() == "can write code"


def test_read_all_collections_surfaces_every_collection(tmp_path):
    """read_all_collections returns the (name, content) pairs across the whole closed set,
    so the prompt assembly surfaces preferences/capabilities the same way it does facts/people."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="facts", name="db", content="BigQuery"))
    mem.apply_memory_op(Remember(collection="preferences", name="theme", content="dark mode"))
    mem.apply_memory_op(Remember(collection="capabilities", name="coding", content="can write code"))
    flat = dict(mem.read_all_collections())
    assert flat["db"] == "BigQuery"
    assert flat["theme"] == "dark mode"
    assert flat["coding"] == "can write code"


def test_rewrite_summary_overwrites(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(RewriteSummary(content="first"))
    mem.apply_memory_op(RewriteSummary(content="second"))
    assert mem.read_summary() == "second"  # running summary is replace, not append


# --- Task 1: the closed memory-op vocabulary (AC1) ---


def test_memory_op_structs_forbid_unknown_fields():
    """A typo'd field is a decode error — the schemas are closed (AD-6)."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b'{"type":"rewrite_about","content":"x","oops":1}', type=MemoryOp)


def test_memory_op_union_rejects_unknown_op_tag():
    """A typo'd op name (`remembr`) is not in the closed union — decode fails."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b'{"type":"remembr","content":"x"}', type=MemoryOp)


def test_memory_op_union_round_trips_by_tag():
    """Each op decodes back to its own type via the tag (the wire's future contract)."""
    for op in (Remember(collection="facts", name="A", content="c"), RewriteAbout(content="c"), LogEpisode(content="c")):
        assert msgspec.json.decode(msgspec.json.encode(op), type=MemoryOp) == op


# --- Task 2 / AC1: apply writes the tree atomically; invalid ops are rejected ---


def test_rewrite_about_writes_about_md(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(RewriteAbout(content="Elliot likes momentum over process."))
    assert (tmp_path / "memory" / "about.md").read_text() == "Elliot likes momentum over process."


def test_invalid_collection_rejected_without_writing(tmp_path):
    """A bad `collection` (bypasses the Literal on direct construction) is rejected by
    core on apply, with nothing written."""
    mem = _mem(tmp_path)
    with pytest.raises(ValueError):
        mem.apply_memory_op(Remember(collection="bogus", name="x", content="c"))
    # The rejected op writes nothing for its target. (The root itself now exists — Story 10.1
    # seeds persona files on CuratedMemory init — so assert the OP's collection dir is absent.)
    assert not (tmp_path / "memory" / "bogus").exists()


def test_empty_content_rejected_without_writing(tmp_path):
    mem = _mem(tmp_path)
    with pytest.raises(ValueError):
        mem.apply_memory_op(RewriteAbout(content="   "))
    assert not (tmp_path / "memory" / "about.md").exists()


def test_unknown_op_type_rejected(tmp_path):
    """A non-MemoryOp object hits the dispatch fall-through and is rejected."""
    mem = _mem(tmp_path)
    with pytest.raises(ValueError):
        mem.apply_memory_op(object())


def test_atomic_write_leaves_prior_about_on_crash(tmp_path, monkeypatch):
    """AD-10: a write interrupted before os.replace leaves the prior file intact and no
    stray temp behind (mirrors the state.py/faces.py crash test)."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(RewriteAbout(content="first"))
    about = tmp_path / "memory" / "about.md"
    good = about.read_text()

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        mem.apply_memory_op(RewriteAbout(content="second"))

    assert about.read_text() == good  # prior doc intact
    # No stray temp left behind. (The root also holds the Story 10.1 persona seeds; assert only
    # that no `.tmp` artifact from the crashed write survived.)
    assert not [p for p in (tmp_path / "memory").iterdir() if p.suffix == ".tmp" or ".tmp" in p.name]


# --- AC2: rewrite_about persists + reads back ---


def test_rewrite_about_persists_and_reads_back(tmp_path):
    CuratedMemory(tmp_path / "memory").apply_memory_op(RewriteAbout(content="durable self-summary"))
    # A fresh instance over the same root sees the persisted doc.
    assert CuratedMemory(tmp_path / "memory").read_about() == "durable self-summary"


def test_read_about_none_when_absent(tmp_path):
    assert _mem(tmp_path).read_about() is None


# --- AC3: DIRECTIVE.md is read-only + disjoint from every write path ---


def test_read_directive_returns_content_when_present(tmp_path):
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    (root / "DIRECTIVE.md").write_text("Be kind. Never lie.")
    assert CuratedMemory(root).read_directive() == "Be kind. Never lie."


def test_read_directive_none_when_absent(tmp_path):
    assert _mem(tmp_path).read_directive() is None


def test_no_memory_op_writes_directive(tmp_path):
    """Apply every op type; DIRECTIVE.md is never created or touched (disjoint writers)."""
    root = tmp_path / "memory"
    mem = CuratedMemory(root)
    mem.apply_memory_op(RewriteAbout(content="about"))
    mem.apply_memory_op(Remember(collection="people", name="Alex", content="friend"))
    mem.apply_memory_op(Remember(collection="facts", name="coffee", content="black"))
    mem.apply_memory_op(LogEpisode(content="a thing happened"))
    mem.apply_memory_op(RewriteSummary(content="a running summary"))  # Story 6.2 op too
    assert not (root / "DIRECTIVE.md").exists()


def test_owner_directive_survives_memory_ops(tmp_path):
    """An owner-authored DIRECTIVE.md is never overwritten by core's writes."""
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    (root / "DIRECTIVE.md").write_text("owner-only constitution")
    mem = CuratedMemory(root)
    mem.apply_memory_op(RewriteAbout(content="bot self-summary"))
    assert (root / "DIRECTIVE.md").read_text() == "owner-only constitution"


# --- AC4: people/ and facts/ placement + path-traversal safety ---


def test_remember_person_creates_people_file(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="people", name="Alex", content="owner's friend"))
    assert (tmp_path / "memory" / "people" / "alex.md").read_text() == "owner's friend"


def test_remember_fact_creates_facts_file(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="facts", name="Favorite Color", content="green"))
    assert (tmp_path / "memory" / "facts" / "favorite-color.md").read_text() == "green"


def test_remember_name_traversal_cannot_escape_tree(tmp_path):
    """`../` and separators slugify to a safe in-tree name — nothing is written outside
    the people/ dir (and the parent is left clean)."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="people", name="../../etc/passwd", content="x"))
    people = tmp_path / "memory" / "people"
    written = list(people.iterdir())
    assert written == [people / "etc-passwd.md"]
    assert not (tmp_path / "etc").exists()  # never escaped the root


def test_remember_unicode_name_persists(tmp_path):
    """A non-ASCII owner-mentioned name is preserved, not rejected (Unicode-safe policy)."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="people", name="José", content="neighbour"))
    mem.apply_memory_op(Remember(collection="people", name="你好", content="from the cafe"))
    names = sorted(p.name for p in (tmp_path / "memory" / "people").iterdir())
    assert names == ["josé.md", "你好.md"]


def test_remember_same_name_overwrites(tmp_path):
    """Same name → one file that accretes the latest content (intended curation)."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(Remember(collection="people", name="Alex", content="first"))
    mem.apply_memory_op(Remember(collection="people", name="alex", content="updated"))
    people = tmp_path / "memory" / "people"
    assert [p.name for p in people.iterdir()] == ["alex.md"]
    assert (people / "alex.md").read_text() == "updated"


def test_remember_name_all_separators_rejected(tmp_path):
    """A name that slugifies to empty has no safe filename — reject, write nothing."""
    mem = _mem(tmp_path)
    with pytest.raises(ValueError):
        mem.apply_memory_op(Remember(collection="people", name="../", content="x"))
    assert not (tmp_path / "memory" / "people").exists()


# --- LogEpisode: appends an episodes record ---


def test_log_episode_appends_records(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(LogEpisode(content="first episode"))
    mem.apply_memory_op(LogEpisode(content="second episode", tags=("walk",)))
    text = (tmp_path / "memory" / "episodes.md").read_text()
    assert "first episode" in text and "second episode" in text
    assert text.index("first episode") < text.index("second episode")  # chronological
    assert "tags: walk" in text
