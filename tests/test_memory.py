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

from shelldon.contracts import LogEpisode, MemoryOp, Remember, RewriteAbout
from shelldon.core.memory import CuratedMemory


def _mem(tmp_path):
    return CuratedMemory(tmp_path / "memory")


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
    assert not (tmp_path / "memory").exists()


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
    assert list((tmp_path / "memory").iterdir()) == [about]  # no stray temp


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
