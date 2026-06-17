"""Story 1.9 — endurance: sustained turns without memory growth (NFR2).

Two complementary proofs (see the story's "why two tests"):

1. IN-PROCESS (cross-platform, always runs): the worker runs as an asyncio task in
   THIS process, so there is no child RSS to reclaim — this test CANNOT prove RAM
   reclamation. What it DOES prove is the complementary half: core/arbiter/fence/bus
   do not accumulate across 500+ turns (bounded internal state + bounded Python
   heap), and ≤1 worker under sustained load (corroborates 1.5 / AD-9).

2. REAL-FORK (Linux-gated, runs on the Pi / CI): real children fork, run,
   os._exit(0), and get waitpid-reaped; the PARENT's /proc/self/statm RSS stays
   flat. This is the only place AC1's "workers spawn and die and RAM is reclaimed"
   is truly observable. Skipped on macOS exactly like tests/test_forkserver_fork.py.

Drive an extended soak with e.g. `SHELLDON_SOAK_TURNS=5000 uv run pytest -m soak`.
"""

import asyncio
import gc
import os
import sys
import tracemalloc
from statistics import median  # stdlib: raises StatisticsError on empty input

import pytest

from shelldon.broker.service import run_broker
from shelldon.contracts import Actor
from shelldon.core.runtime import Core
from shelldon.display.renderer import StubRenderer
from shelldon.display.service import run_display
from shelldon.transport.cli import run_cli_transport
from shelldon.worker.forkserver import ForkServer

# Reuse the Story 1.8 in-process harness verbatim (no __init__.py in tests/ → pytest
# prepend mode makes the sibling module importable by basename).
# NOTE: _Source, _await are private helpers from the 1.8 harness — if they're renamed
# in test_end_to_end_turn.py, update this import (a rename breaks collection here).
from test_end_to_end_turn import OkProvider, Spawns, _Source, _await, build_harness

#: Default keeps the normal `pytest` run fast; override for an extended manual soak.
SOAK_TURNS = int(os.environ.get("SHELLDON_SOAK_TURNS", "500"))

#: In-process steady-state Python-heap growth ceiling (bytes), measured by
#: tracemalloc AFTER a warmup with test-side sinks cleared each turn (so it's
#: N-independent). Observed steady state ~73 KB at both 500 and 2000 turns (the
#: saturated 256-entry TurnFence history + tracemalloc overhead); this bound keeps
#: ~3.4× headroom. A real per-turn leak in core/arbiter/fence/bus breaches it.
IN_PROCESS_HEAP_BOUND = 250_000  # 250 KB

#: Real-fork parent-RSS spread ceiling (KiB): max minus min of the four post-warmup
#: quartile medians. Generous on first landing — TIGHTEN after the first real Pi/Linux
#: run once the true steady-state jiggle is known.
RSS_FLAT_BOUND_KB = 10 * 1024  # 10 MiB

#: A monotonic climb across ALL four quartiles by more than this is a leak signal even
#: when it stays under RSS_FLAT_BOUND_KB. Above steady-state noise so flat data (tiny
#: spread) and single transient spikes (not all-ascending) don't trip it.
RSS_CLIMB_FLOOR_KB = 1024  # 1 MiB


def _rss_kb() -> int:
    """Current resident set size of THIS process in KiB (Linux /proc/self/statm).

    Field index 1 (the second value) is resident pages; × page size. This is the
    only stdlib source of *current* RSS that can show a flat-vs-climbing trend
    (resource.ru_maxrss is peak-only). Linux-only — the caller is Linux-gated.
    """
    with open("/proc/self/statm") as f:
        resident_pages = int(f.read().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE") // 1024


def _at_rest(core: Core) -> bool:
    """True when a turn is fully wound down: the arbiter policy slot is released AND
    the ForkServer mechanical guard is released (worker reaped). Feeding the next
    message only when at rest keeps it one-turn-per-message (no coalescing), so the
    soak's per-turn accounting is deterministic."""
    return not core.arbiter.worker_in_flight and not core.spawner.worker_in_flight


@pytest.mark.soak
async def test_in_process_core_does_not_accumulate(sock_path):
    """500+ turns in-process: ≤1 worker under load, bounded core state, flat heap.

    Proves the in-process half of NFR2 — core/arbiter/fence/bus do not accumulate.
    (RAM reclamation itself needs the real fork; see the Linux-gated test below.)
    """
    spawns = Spawns()
    h = await build_harness(sock_path, provider=OkProvider(), spawns=spawns)
    warmup = min(20, SOAK_TURNS // 5)
    delivered = 0
    try:
        async def one_turn(i: int) -> int:
            # Clear test-side sinks BEFORE the turn so retention is O(1), not O(N):
            # the outbound list and the display stub's `rendered` list legitimately
            # grow per turn, which would swamp the heap signal we care about (core
            # accumulation). Clearing isolates what core/bus/runtime retain.
            h.outbound.clear()
            h.renderer.rendered.clear()
            h.source.feed(f"msg {i}")
            await _await(lambda: len(h.outbound) == 1, timeout=5.0)
            await _await(lambda: _at_rest(h.core), timeout=5.0)
            return len(h.outbound)  # must be exactly 1 — no coalescing, nothing dropped

        # Warmup turns absorb one-time lazy allocations before we start measuring.
        for i in range(warmup):
            delivered += await one_turn(i)

        gc.collect()
        tracemalloc.start()
        samples: list[int] = [tracemalloc.get_traced_memory()[0]]  # baseline ≈ 0
        for i in range(warmup, SOAK_TURNS):
            n = await one_turn(i)
            assert n == 1
            delivered += n
            if i % 50 == 0:
                samples.append(tracemalloc.get_traced_memory()[0])
        gc.collect()
        samples.append(tracemalloc.get_traced_memory()[0])
        # Steady-state growth = last (steady) minus first (baseline ≈ 0). Unlike
        # max(samples), this won't fire on a one-time early alloc that later frees,
        # and a real per-turn leak makes the last sample grow with N.
        heap_delta = samples[-1] - samples[0]
        tracemalloc.stop()
        print(f"\n[soak in-process] turns={SOAK_TURNS} heap_delta={heap_delta} bytes")

        # ≤1 worker under sustained load (AC2 corroboration).
        assert spawns.count == SOAK_TURNS
        assert spawns.max_live == 1
        # One reply per turn — nothing dropped or coalesced across the whole run.
        assert delivered == SOAK_TURNS
        # No unbounded core state across the whole run.
        assert h.core.arbiter._pending == []
        assert h.core.arbiter.worker_in_flight is False
        assert h.core.fence.current is None
        # eviction logic in TurnFence.close() keeps _closed (a set) bounded ≤256
        assert len(h.core.fence._closed) <= 256
        # Background reap tasks don't accumulate. _at_rest already implies the last
        # reap reached its finally, but the done-callback discard can lag a tick — a
        # short poll confirms the _bg set fully drains over the whole run.
        await _await(lambda: len(h.core._bg) == 0, timeout=5.0)
        assert h.core._seq == 2 * SOAK_TURNS  # thinking + reply face per turn, exactly
        # No monotonic Python-heap growth in core/bus/runtime (AC1, in-process half).
        # N-independent: test-side O(N) retention is cleared each turn (above), so a
        # breach means real accumulation, not bookkeeping.
        assert heap_delta < IN_PROCESS_HEAP_BOUND, (
            f"heap grew {heap_delta} bytes over {SOAK_TURNS} turns "
            f"(bound {IN_PROCESS_HEAP_BOUND}) — possible accumulation in core"
        )
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        await h.teardown()


@pytest.mark.soak
@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="fork-without-exec is unsafe on macOS frameworks; prod target is Linux",
)
async def test_real_fork_rss_stays_flat(sock_path):
    """The true NFR2 proof: real children fork → os._exit(0) → waitpid-reaped, and
    the PARENT's RSS stays flat over a sustained run. Linux/Pi only."""
    fs = ForkServer(sock_path)  # real os.fork() + os.waitpid(); preload freezes GC
    await fs.preload()
    core = Core(sock_path, fs, turn_timeout=10.0)

    source = _Source()
    outbound: list[str] = []
    renderer = StubRenderer()

    async def sink(text: str) -> None:
        outbound.append(text)

    tasks = [
        asyncio.create_task(core.run()),
        asyncio.create_task(run_broker(sock_path, OkProvider())),
        asyncio.create_task(run_display(sock_path, renderer)),
        asyncio.create_task(run_cli_transport(sock_path, inbound=source, outbound=sink)),
    ]
    warmup = min(20, SOAK_TURNS // 5)
    try:
        await _await(lambda: core.bus._server is not None)
        await _await(
            lambda: all(
                core.bus._registry.get(a) is not None
                for a in (Actor.BROKER, Actor.DISPLAY, Actor.CHAT_TRANSPORT)
            )
        )

        rss: list[int] = []
        for i in range(SOAK_TURNS):
            source.feed(f"msg {i}")
            await _await(lambda: len(outbound) == i + 1, timeout=15.0)
            await _await(lambda: _at_rest(core), timeout=15.0)
            if i >= warmup:
                rss.append(_rss_kb())

        # ≤1 + reclaimed: a clean run with no WorkerBusyError proves ≤1 under load
        # (the mechanical guard would have raised), and the guard is released at end.
        assert fs.worker_in_flight is False
        assert len(outbound) == SOAK_TURNS

        # Flatness over four post-warmup quartiles. A reclaiming design has no per-turn
        # parent growth, so the spread is just noise.
        assert rss, f"no RSS samples collected (SOAK_TURNS={SOAK_TURNS}, warmup={warmup})"
        assert len(rss) >= 4, f"need >=4 post-warmup samples for quartiles (got {len(rss)})"
        q = len(rss) // 4
        medians = [median(rss[k * q:(k + 1) * q]) for k in range(4)]
        spread = max(medians) - min(medians)
        climbing = all(a < b for a, b in zip(medians, medians[1:]))
        print(f"\n[soak real-fork] turns={SOAK_TURNS} quartile_medians={medians}KiB "
              f"spread={spread}KiB climbing={climbing}")
        # Absolute ceiling — catches any spike or climb.
        assert spread < RSS_FLAT_BOUND_KB, (
            f"parent RSS spread {spread} KiB across the soak (bound {RSS_FLAT_BOUND_KB}) "
            f"— workers may not be reclaiming"
        )
        # Sub-ceiling monotonic climb: growth across ALL quartiles by a meaningful
        # amount is a leak even under the ceiling. Flat/equal data (tiny spread) and a
        # single transient spike (not all-ascending) don't trip this — unlike a naive
        # strict-ascending check, which false-fails on flat RSS.
        assert not (climbing and spread > RSS_CLIMB_FLOOR_KB), (
            f"parent RSS climbed monotonically across all quartiles {medians} KiB "
            f"(spread {spread} > {RSS_CLIMB_FLOOR_KB}) — likely a per-turn leak"
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await core.bus.stop()
        finally:
            gc.unfreeze()  # preload() froze; ALWAYS restore even if stop() raised
            gc.enable()
