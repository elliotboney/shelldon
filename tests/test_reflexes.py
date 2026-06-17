"""Story 3.2 — the resident reflex loop.

Covers the pure reflex policy `compute_reflex_patch` (deterministic, no I/O — the
unit Epic 5's scheduler subsumes, AC3), the in-core reflex tick driver that applies
it via the Story 3.1 `apply_patch` API with no LLM/network (AC1), and the turn-path
`last_interaction` write that feeds the idle signal — all through the single-writer
core path (AC2).

Pure-function tests use fixed `now` timestamps (no wall-clock, no sleeps). Tick tests
inject a tiny interval and poll a state predicate (Epic 2 retro #1). Every Core uses
an injected `tmp_path` checkpoint — never real `$HOME`.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from conftest import DummySpawner, await_true
from shelldon.core.reflexes import compute_reflex_patch
from shelldon.core.runtime import Core
from shelldon.core.state import Mood, PersonalityState


def _night() -> datetime:
    return datetime(2026, 6, 17, 2, 0, tzinfo=UTC)  # arousal target low (calm)


def _midday() -> datetime:
    return datetime(2026, 6, 17, 12, 0, tzinfo=UTC)  # arousal target high (lively)


_ANCIENT = "2000-01-01T00:00:00+00:00"  # long idle


class _RaisingSpawner:
    """Every method raises — proves the reflex tick never touches the brain/network."""

    async def ready(self):
        raise AssertionError("reflex path must not call the spawner")

    async def spawn_turn(self, turn_id, prompt):
        raise AssertionError("reflex path must not spawn a worker")

    async def reap_current(self):
        raise AssertionError("reflex path must not reap")


# --- AC1: the pure reflex function (deterministic, bounded, no mutation) ---


def test_time_of_day_drifts_arousal_down_at_night():
    s = PersonalityState(mood=Mood(valence=0.0, arousal=0.5))
    patch = compute_reflex_patch(s, _night())
    assert patch["mood.arousal"] < 0.5  # nudged toward the calm night target
    assert -1.0 <= patch["mood.arousal"] <= 1.0


def test_time_of_day_drifts_arousal_up_at_midday():
    s = PersonalityState(mood=Mood(valence=0.0, arousal=-0.5))
    patch = compute_reflex_patch(s, _midday())
    assert patch["mood.arousal"] > -0.5  # nudged toward the lively midday target


def test_idle_settles_valence_and_energy_toward_baseline():
    s = PersonalityState(mood=Mood(valence=0.9, arousal=0.0), energy=0.95, last_interaction=_ANCIENT)
    patch = compute_reflex_patch(s, _midday())
    assert patch["mood.valence"] < 0.9  # high mood fades toward neutral when ignored
    assert patch["energy"] < 0.95  # energy settles toward the resting baseline
    assert -1.0 <= patch["mood.valence"] <= 1.0
    assert 0.0 <= patch["energy"] <= 1.0


def test_recent_interaction_produces_no_idle_drift():
    now = _midday()
    s = PersonalityState(mood=Mood(valence=0.9, arousal=0.5), energy=0.95, last_interaction=now.isoformat())
    patch = compute_reflex_patch(s, now)
    assert "mood.valence" not in patch  # just interacted — no settling
    assert "energy" not in patch


def test_no_last_interaction_means_no_idle_drift():
    s = PersonalityState(mood=Mood(valence=0.9, arousal=0.0), energy=0.95, last_interaction=None)
    patch = compute_reflex_patch(s, _midday())
    assert "mood.valence" not in patch
    assert "energy" not in patch


def test_unusable_last_interaction_is_ignored_not_raised():
    """A garbage or tz-naive last_interaction (e.g. a hand-edited checkpoint) yields
    no idle drift and never raises — treated as 'no idle signal' (review #2/#5)."""
    for bad in ("not-a-timestamp", "2026-06-17T12:00:00"):  # garbage, then tz-naive
        s = PersonalityState(mood=Mood(valence=0.9, arousal=0.0), energy=0.95, last_interaction=bad)
        patch = compute_reflex_patch(s, _midday())
        assert "mood.valence" not in patch
        assert "energy" not in patch


def test_at_rest_returns_empty_patch():
    """Already at the time-of-day target AND just interacted -> nothing to do."""
    now = _midday()
    s = PersonalityState(mood=Mood(valence=0.0, arousal=0.5), energy=0.5, last_interaction=now.isoformat())
    assert compute_reflex_patch(s, now) == {}


def test_reflex_patch_keys_are_all_writable_paths():
    from shelldon.core.state import WRITABLE_PATHS

    s = PersonalityState(mood=Mood(valence=0.9, arousal=0.9), energy=0.9, last_interaction=_ANCIENT)
    patch = compute_reflex_patch(s, _night())
    assert patch  # this state must produce some drift
    assert set(patch).issubset(WRITABLE_PATHS)  # never a path apply_patch would reject


# --- AC3: pure + standalone (what the Epic 5 scheduler will call) ---


def test_reflex_function_is_deterministic_and_pure():
    s = PersonalityState(mood=Mood(valence=0.3, arousal=0.5), energy=0.7, last_interaction=_ANCIENT)
    p1 = compute_reflex_patch(s, _night())
    p2 = compute_reflex_patch(s, _night())
    assert p1 == p2  # deterministic for a fixed (state, now)
    # Pure: computing the patch did NOT mutate the input state.
    assert s.mood.arousal == 0.5
    assert s.mood.valence == 0.3
    assert s.energy == 0.7


# --- AC1: the in-core tick applies the patch, offline, no LLM ---


async def test_reflex_tick_mutates_state_offline(sock_path, tmp_path):
    """The tick drifts RAM state with a spawner that raises if touched — proving the
    reflex path never calls the brain/broker (works network-down)."""
    core = Core(
        sock_path, _RaisingSpawner(), checkpoint_path=tmp_path / "state.json", reflex_interval=0.01
    )
    core.state.apply_patch({"mood.valence": 0.9, "last_interaction": _ANCIENT})
    before = core.state.state.mood.valence

    task = asyncio.create_task(core._reflex_loop())
    try:
        await await_true(lambda: core.state.state.mood.valence < before)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_reflex_tick_marks_dirty_for_the_311_flush(sock_path, tmp_path):
    """A reflex write marks the struct dirty — the seam the 3.1 checkpoint loop flushes
    (3.2 adds no new disk write)."""
    core = Core(
        sock_path, _RaisingSpawner(), checkpoint_path=tmp_path / "state.json", reflex_interval=0.01
    )
    core.state.apply_patch({"mood.valence": 0.9, "last_interaction": _ANCIENT})
    core.state.checkpoint(core.checkpoint_path)  # clear dirty
    assert core.state.dirty is False

    task = asyncio.create_task(core._reflex_loop())
    try:
        await await_true(lambda: core.state.dirty is True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_reflex_loop_survives_an_error(sock_path, tmp_path, monkeypatch):
    """One bad tick must not permanently kill reflexes (3.1 review precedent)."""
    core = Core(
        sock_path, _RaisingSpawner(), checkpoint_path=tmp_path / "state.json", reflex_interval=0.01
    )
    core.state.apply_patch({"mood.valence": 0.9, "last_interaction": _ANCIENT})

    real = core.state.apply_patch
    calls = {"n": 0}

    def flaky(patch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated bad tick")
        return real(patch)

    monkeypatch.setattr(core.state, "apply_patch", flaky)

    task = asyncio.create_task(core._reflex_loop())
    try:
        await await_true(lambda: calls["n"] >= 2)  # kept ticking past the error
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_nonpositive_reflex_interval_rejected(sock_path):
    with pytest.raises(ValueError):
        Core(sock_path, DummySpawner(), reflex_interval=0)
    with pytest.raises(ValueError):
        Core(sock_path, DummySpawner(), reflex_interval=-1.0)


# --- AC2: turn-path last_interaction write + single-writer coexistence ---


def test_mark_interaction_sets_last_interaction(sock_path, tmp_path):
    core = Core(sock_path, DummySpawner(), checkpoint_path=tmp_path / "state.json")
    assert core.state.state.last_interaction is None

    core._mark_interaction()
    li = core.state.state.last_interaction
    assert li is not None
    datetime.fromisoformat(li)  # a parseable ISO-8601 timestamp
    assert core.state.dirty is True


def test_turn_write_and_reflex_write_coexist_single_writer(sock_path, tmp_path):
    """The turn path and the reflex tick both mutate through the one apply_patch —
    no field clobbers another; the struct stays consistent (AC2)."""
    core = Core(sock_path, DummySpawner(), checkpoint_path=tmp_path / "state.json")
    core._mark_interaction()
    li = core.state.state.last_interaction

    patch = compute_reflex_patch(core.state.state, _night())
    assert patch  # default state at night drifts arousal
    core.state.apply_patch(patch)

    # Reflex updated mood/energy; the turn's last_interaction is untouched.
    assert core.state.state.last_interaction == li
    assert core.state.state.mood.arousal == patch["mood.arousal"]
