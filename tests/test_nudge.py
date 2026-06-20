"""Story 7.5 — the core nudge handler + the end-to-end CAP (a plugin event moves the soul).

The handler is reflex-tier: it maps a plugin-emitted affect (`NUDGE_*`) to a bounded mood
patch via `core/reactions.py`, debounces per-kind on a cooldown, applies through the
single-writer `apply_patch`, and re-renders the mood face BETWEEN turns — no arbiter, no
fork, no LLM, no budget. The CAP test proves the whole wire: a `NUDGE_EXCITED` emitted onto
the bus by a plugin-host connection ends as an `excited` FACE snapshot at the display.
"""

import asyncio

import pytest

from shelldon.contracts import (
    Actor,
    Envelope,
    Event,
    EventKind,
    MsgKind,
    Region,
)
from shelldon.core.bus import connect, read_frame, write_frame
from shelldon.core.runtime import Core
from tests.conftest import DummySpawner, await_true


class _Clock:
    """A hand-cranked monotonic clock for the cooldown tests (no real sleeps)."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- the handler (called directly; no bus loop needed) --------------------------


async def test_nudge_applies_clamped_patch_and_repushes_the_mood_face_when_idle(sock_path):
    core = Core(sock_path, DummySpawner())
    # Sit just below the `excited` face (valence/arousal >= 0.4, energy >= 0.5).
    core.state.apply_patch({"mood.valence": 0.4, "mood.arousal": 0.3})

    await core._handle_nudge(EventKind.NUDGE_EXCITED)

    assert core.state.state.mood.arousal == pytest.approx(0.6)
    assert core.state.state.mood.valence == pytest.approx(0.5)
    assert core._last_face == "excited"  # the face reacted (idle -> pushed immediately)


async def test_nudge_cooldown_drops_a_repeat_of_the_same_kind_within_the_window(sock_path):
    clock = _Clock()
    core = Core(sock_path, DummySpawner(), monotonic=clock)  # default 30s cooldown

    await core._handle_nudge(EventKind.NUDGE_POSITIVE)            # t=0 -> applies
    assert core.state.state.mood.valence == pytest.approx(0.3)

    clock.t = 10.0
    await core._handle_nudge(EventKind.NUDGE_POSITIVE)            # within 30s -> dropped
    assert core.state.state.mood.valence == pytest.approx(0.3)

    clock.t = 40.0
    await core._handle_nudge(EventKind.NUDGE_POSITIVE)            # past the window -> applies
    assert core.state.state.mood.valence == pytest.approx(0.6)


async def test_distinct_kinds_have_independent_cooldowns(sock_path):
    clock = _Clock()
    core = Core(sock_path, DummySpawner(), monotonic=clock)

    await core._handle_nudge(EventKind.NUDGE_POSITIVE)   # valence +0.3
    await core._handle_nudge(EventKind.NUDGE_CALM)       # arousal -0.3 (different kind, not gated)

    assert core.state.state.mood.valence == pytest.approx(0.3)
    assert core.state.state.mood.arousal == pytest.approx(-0.3)


async def test_nudge_mid_turn_applies_the_patch_but_does_not_push_a_face(sock_path):
    core = Core(sock_path, DummySpawner())
    core.state.apply_patch({"mood.valence": 0.4, "mood.arousal": 0.3})
    core.arbiter.submit("busy")  # a turn is in flight -> the arbiter is no longer idle

    await core._handle_nudge(EventKind.NUDGE_EXCITED)

    assert core.state.state.mood.arousal == pytest.approx(0.6)  # mood still moved
    assert core._last_face is None                              # but the face was NOT re-pushed


async def test_unknown_event_kind_is_a_no_op(sock_path):
    core = Core(sock_path, DummySpawner())
    before_v = core.state.state.mood.valence
    before_a = core.state.state.mood.arousal

    await core._handle_nudge(EventKind.MESSAGE_ANSWERED)  # core's own kind, not an affect

    assert core.state.state.mood.valence == before_v
    assert core.state.state.mood.arousal == before_a
    assert core._last_face is None


async def test_nudge_does_not_touch_last_interaction(sock_path):
    core = Core(sock_path, DummySpawner())
    core.state.apply_patch({"last_interaction": "2026-06-19T00:00:00+00:00"})

    await core._handle_nudge(EventKind.NUDGE_POSITIVE)

    # A nudge moves mood only — it must NOT reset the proactive idle clock.
    assert core.state.state.last_interaction == "2026-06-19T00:00:00+00:00"


# --- CAP: a plugin-emitted event visibly moves the pet's soul -------------------


async def test_cap_plugin_nudge_drives_the_face_to_excited(sock_path):
    """End-to-end: a plugin-host connection emits NUDGE_EXCITED onto the bus -> the hub
    routes it to core (Story 7.5) -> the runtime's handler nudges mood -> an `excited`
    FACE snapshot reaches the display. Proves the whole wire, off the same bus stream."""
    # Park the background jobs (the background-emitter rule) so no reflex tick drifts the
    # mood face — only the nudge should produce a FACE snapshot.
    core = Core(sock_path, DummySpawner(), reflex_interval=3600, scheduler_interval=3600)
    core.state.apply_patch({"mood.valence": 0.4, "mood.arousal": 0.3})  # poised below `excited`
    run_task = asyncio.create_task(core.run())
    try:
        await await_true(lambda: core.bus._server is not None)  # bus listening
        # Keep BOTH writers referenced: a dropped StreamWriter is GC'd and closes the
        # connection, deregistering the actor (so don't `_`-discard the display writer).
        d_reader, _d_writer = await connect(sock_path, Actor.DISPLAY)
        _ph_reader, ph_writer = await connect(sock_path, Actor.PLUGIN_HOST)
        await await_true(lambda: core.bus._registry.get(Actor.DISPLAY) is not None)

        await write_frame(
            ph_writer,
            Envelope(
                id="nudge-1",
                kind=MsgKind.EVENT,
                src=Actor.PLUGIN_HOST,
                dst=None,
                body=Event(event=EventKind.NUDGE_EXCITED),
            ),
        )

        snap = await asyncio.wait_for(read_frame(d_reader), timeout=2.0)
        assert snap.kind is MsgKind.STATE_SNAPSHOT
        assert snap.body.region is Region.FACE
        assert snap.body.face == "excited"
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
