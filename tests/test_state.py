"""Story 3.1 — the persistent personality-state substrate.

Covers the struct + closed dotted-path patch writer (AD-5), the first atomic
checkpoint in the tree (temp + fsync + os.replace, AD-10), corruption-tolerant
restore (AC3), and the in-core periodic flush (AC2 "not per change", NFR7).

Every test injects a `tmp_path`-based checkpoint file — never real `$HOME`.
Assertions are on RAM values, dirty/clean transitions, and file presence/contents
(state predicates, not sleep anchors — Epic 2 retro action #1).
"""

import asyncio
import os
from pathlib import Path

import msgspec
import pytest

from conftest import DummySpawner, await_true
from shelldon.core.runtime import Core
from shelldon.core.state import WRITABLE_PATHS, PersistentState, PersonalityState


# --- AC1: load defaults on first run, restore from a checkpoint ---


def test_first_run_defaults_no_file(tmp_path):
    """No checkpoint yet -> sane defaults, no crash, no file created."""
    target = tmp_path / "state.json"
    ps = PersistentState.load(target)

    assert ps.state.energy == pytest.approx(0.5)
    assert ps.state.mood.valence == pytest.approx(0.0)
    assert ps.state.last_interaction is None
    assert ps.dirty is False
    assert not target.exists()  # load must not write


def test_checkpoint_then_reload_restores_values(tmp_path):
    """A valid checkpoint round-trips: a fresh PersistentState restores the values."""
    target = tmp_path / "state.json"
    ps = PersistentState.load(target)
    ps.apply_patch({"mood.valence": 0.7, "energy": 0.3, "last_interaction": "2026-06-17T00:00:00Z"})
    ps.checkpoint(target)

    restored = PersistentState.load(target)
    assert restored.state.mood.valence == pytest.approx(0.7)
    assert restored.state.energy == pytest.approx(0.3)
    assert restored.state.last_interaction == "2026-06-17T00:00:00Z"
    assert restored.dirty is False


# --- AC2: sparse patches over a closed set; dirty flag; not-on-every-change ---


def test_apply_patch_updates_ram_and_marks_dirty(tmp_path):
    ps = PersistentState.load(tmp_path / "state.json")
    assert ps.dirty is False

    ps.apply_patch({"mood.valence": -0.4, "mood.arousal": 0.2})
    assert ps.state.mood.valence == pytest.approx(-0.4)
    assert ps.state.mood.arousal == pytest.approx(0.2)
    assert ps.dirty is True


def test_unknown_path_rejected_and_applies_nothing(tmp_path):
    """An unknown dotted path is rejected whole — no half-apply, no silent attr,
    state stays clean (the Region-enum typo-rejection precedent)."""
    ps = PersistentState.load(tmp_path / "state.json")

    with pytest.raises(KeyError):
        ps.apply_patch({"mood.valence": 0.9, "mood.nope": 1.0})

    # Whole patch rejected: the valid key did NOT apply, and nothing went dirty.
    assert ps.state.mood.valence == pytest.approx(0.0)
    assert ps.dirty is False
    assert not hasattr(ps.state.mood, "nope")


def test_unknown_top_level_path_rejected(tmp_path):
    ps = PersistentState.load(tmp_path / "state.json")
    with pytest.raises(KeyError):
        ps.apply_patch({"hp": 100})
    assert ps.dirty is False


def test_closed_set_is_the_documented_paths():
    assert WRITABLE_PATHS == {"mood.valence", "mood.arousal", "energy", "last_interaction"}


def test_patch_does_not_write_to_disk(tmp_path):
    """The core promise: a patch mutates RAM only — disk is untouched until an
    explicit checkpoint. Proves 'not on every change'."""
    target = tmp_path / "state.json"
    ps = PersistentState.load(target)

    ps.apply_patch({"energy": 0.1})
    assert not target.exists()  # still nothing on disk after a mutation

    ps.checkpoint(target)
    assert target.exists()  # only now


def test_checkpoint_clears_dirty(tmp_path):
    target = tmp_path / "state.json"
    ps = PersistentState.load(target)
    ps.apply_patch({"energy": 0.1})
    assert ps.dirty is True
    ps.checkpoint(target)
    assert ps.dirty is False


# --- AC2/AC3: atomic write — the first atomic write in the tree (AD-10) ---


def test_checkpoint_creates_parent_dir(tmp_path):
    target = tmp_path / "nested" / "deep" / "state.json"
    ps = PersistentState.load(target)
    ps.apply_patch({"energy": 0.2})
    ps.checkpoint(target)
    assert target.exists()


def test_interrupted_checkpoint_leaves_prior_file_intact(tmp_path, monkeypatch):
    """AD-10 invariant: a write interrupted before os.replace leaves the prior good
    checkpoint untouched — never a half-written file the loader sees. dirty stays set."""
    target = tmp_path / "state.json"
    ps = PersistentState.load(target)
    ps.apply_patch({"energy": 0.9})
    ps.checkpoint(target)
    good = target.read_bytes()

    # A second checkpoint where the atomic rename "crashes" mid-write.
    ps.apply_patch({"energy": 0.1})

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ps.checkpoint(target)

    # Prior good file is byte-for-byte intact; the failed write left it untouched.
    assert target.read_bytes() == good
    # The crash is not silently swallowed — state is still dirty (unflushed).
    assert ps.dirty is True
    # No stray temp file left littering the directory.
    assert list(tmp_path.iterdir()) == [target]


# --- AC3: corruption tolerance on restore ---


def test_load_garbage_falls_back_to_defaults(tmp_path, caplog):
    target = tmp_path / "state.json"
    target.write_bytes(b"\x00\x01 not json at all }{")

    ps = PersistentState.load(target)
    assert ps.state.energy == pytest.approx(0.5)  # defaults, no raise
    assert ps.state.mood.valence == pytest.approx(0.0)


def test_load_truncated_json_falls_back_to_defaults(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b'{"mood": {"valence": 0.5}, "ene')  # partially written

    ps = PersistentState.load(target)
    assert ps.state.energy == pytest.approx(0.5)
    assert ps.state.last_interaction is None


def test_load_schema_mismatch_falls_back_to_defaults(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(msgspec.json.encode({"mood": {"valence": "not-a-float"}}))

    ps = PersistentState.load(target)
    assert ps.state.mood.valence == pytest.approx(0.0)


def test_stray_temp_file_does_not_break_load(tmp_path):
    """A leftover temp from a prior crash must not shadow a valid target."""
    target = tmp_path / "state.json"
    ps = PersistentState.load(target)
    ps.apply_patch({"energy": 0.42})
    ps.checkpoint(target)
    (tmp_path / "state.json.tmp123").write_bytes(b"garbage leftover")

    restored = PersistentState.load(target)
    assert restored.state.energy == pytest.approx(0.42)


# --- AC2 / NFR7: the in-core periodic flush — fires only when dirty ---


def test_core_restores_state_on_construction(sock_path, tmp_path):
    target = tmp_path / "state.json"
    seed = PersistentState.load(target)
    seed.apply_patch({"energy": 0.25})
    seed.checkpoint(target)

    core = Core(sock_path, DummySpawner(), checkpoint_path=target)
    assert core.state.state.energy == pytest.approx(0.25)


def test_core_flush_writes_only_when_dirty(sock_path, tmp_path):
    target = tmp_path / "state.json"
    core = Core(sock_path, DummySpawner(), checkpoint_path=target)

    # Not dirty -> the periodic flush is a no-op (no file written).
    core._checkpoint_if_dirty()
    assert not target.exists()

    # Dirty -> exactly one write happens and the flag clears.
    core.state.apply_patch({"mood.valence": 0.6})
    assert core.state.dirty is True
    core._checkpoint_if_dirty()
    assert target.exists()
    assert core.state.dirty is False

    # Clean again -> another flush does not rewrite.
    mtime = target.stat().st_mtime_ns
    core._checkpoint_if_dirty()
    assert target.stat().st_mtime_ns == mtime


# --- Review follow-ups: defensive hardening of the flush/restore paths ---


async def test_checkpoint_loop_survives_a_disk_error(sock_path, tmp_path, monkeypatch):
    """A transient disk error in one flush must NOT permanently kill periodic
    checkpointing — the scheduler logs and retries the checkpoint job on the next tick
    (state stays dirty). The checkpoint flush is now a reflex-tier scheduler job (5.1)."""
    target = tmp_path / "state.json"
    core = Core(
        sock_path, DummySpawner(), checkpoint_path=target,
        checkpoint_interval=0.01, scheduler_interval=0.01,
    )
    core.state.apply_patch({"energy": 0.3})

    real = core.state.checkpoint
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk full")
        return real(path)

    monkeypatch.setattr(core.state, "checkpoint", flaky)

    task = asyncio.create_task(core._scheduler_loop())
    try:
        await await_true(lambda: target.exists())  # written despite the first error
        assert calls["n"] >= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_load_unreadable_file_falls_back_to_defaults(tmp_path, monkeypatch):
    """A present-but-unreadable checkpoint (PermissionError / TOCTOU delete after the
    exists() check) falls back to defaults rather than crashing Core.__init__."""
    target = tmp_path / "state.json"
    target.write_bytes(b"{}")

    def boom(self):
        raise PermissionError("unreadable")

    monkeypatch.setattr(Path, "read_bytes", boom)
    ps = PersistentState.load(target)
    assert ps.state.energy == pytest.approx(0.5)
    assert ps.dirty is False


def test_nonpositive_checkpoint_interval_rejected(sock_path):
    """Zero/negative interval would busy-spin asyncio.sleep(0) and starve the loop."""
    with pytest.raises(ValueError):
        Core(sock_path, DummySpawner(), checkpoint_interval=0)
    with pytest.raises(ValueError):
        Core(sock_path, DummySpawner(), checkpoint_interval=-1.0)
