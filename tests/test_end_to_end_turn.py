"""Story 1.8 — the end-to-end turn, all five actors in-process on one BusServer.

The full turn is exercised cross-platform with the worker spawned via the
fork-server's INJECTED spawn seam (`asyncio.create_task(run_worker(...))`) — no real
`os.fork()` (that path stays Linux-gated in 1.5). The provider is always a fake.

This wires the two ≤1 guards the 1.5 review left independent: the **Arbiter** is the
single *policy* gate (it won't request a second spawn until `complete()`), and
**ForkServer.worker_in_flight** is the *mechanical* backstop — they never conflict
because the arbiter serializes turn requests on the single-consumer core loop (which
also makes Arbiter access serial: no lock needed — resolves the 1.5 async-safety item).

Production shape (NOT built here): the fork-server runs as its own single-threaded
process driven by core over an IPC control channel (never fork from the asyncio
loop). 1.8 proves the turn *lifecycle* in-process; multi-process + IPC is a later
deployment-hardening story.
"""

import asyncio

import pytest

from shelldon.broker.provider import TransientProviderError
from shelldon.broker.service import run_broker
from shelldon.contracts import Actor
from shelldon.core.runtime import DEGRADE_TEXT, FACE_DEGRADED, Core
from shelldon.display.renderer import StubRenderer
from shelldon.display.service import run_display
from shelldon.transport.cli import run_cli_transport
from shelldon.worker.forkserver import ForkServer
from shelldon.worker.prompt import SYSTEM_INSTRUCTION
from shelldon.worker.worker import run_worker


# --- controllable inbound source (the 1.6 _Source pattern) ---


class _Source:
    """A controllable inbound line source: `feed()` queues a line, `close()` ends
    the stream. Stays open so the outbound half can be exercised before teardown."""

    def __init__(self):
        self._q: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, line: str) -> None:
        self._q.put_nowait(line)

    def close(self) -> None:
        self._q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


# --- fake providers ---


class OkProvider:
    """Returns a reply immediately."""

    name = "fake"

    async def complete(self, prompt: str) -> str:
        return f"reply to: {prompt}"


class GatedProvider:
    """Blocks every call on a gate so a turn can be held 'in flight'. Once the gate
    is opened it stays open, so later (catch-up) turns complete immediately."""

    name = "fake"

    def __init__(self):
        self.gate = asyncio.Event()
        self.entered = 0

    async def complete(self, prompt: str) -> str:
        self.entered += 1
        await self.gate.wait()
        return f"reply to: {prompt}"


class AlwaysTransientProvider:
    """Raises a transient error on every attempt — forces the broker's single retry
    to exhaust and return Result(ok=False) (AC3 failure path)."""

    name = "fake"

    async def complete(self, prompt: str) -> str:
        raise TransientProviderError("provider down")


class RecoverableProvider:
    """Down while `self.down` is True (raises like an outage), then answers once
    flipped — drives AC3 auto-recovery: a turn degrades during the outage, and the
    NEXT turn completes normally with no latched 'degraded mode' to clear."""

    name = "fake"

    def __init__(self):
        self.down = True

    async def complete(self, prompt: str) -> str:
        if self.down:
            raise TransientProviderError("offline")
        return f"reply to: {prompt}"


# --- in-process spawn seam with a concurrency counter (AC1/AC2) ---


async def _passthrough_worker(socket_path, turn_id, prompt):
    """Lifecycle-harness worker: IDENTITY prompt assembly (Story 4.4) so these tests
    assert on the raw owner message, not the assembled prompt. Real assembly has its
    own tests (test_prompt_assembly.py); the CAP-6 path uses `worker=run_worker`."""
    await run_worker(socket_path, turn_id, prompt, assemble=lambda m: m)


class RecordingProvider:
    """Records every prompt it is asked to complete — used to assert WHAT reached the
    brain (Story 4.4 / CAP-6), not just that a reply came back."""

    name = "fake"

    def __init__(self):
        self.seen: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.seen.append(prompt)
        return "noted"


class Spawns:
    """The in-process fork-server seam: `spawn` runs the worker as an asyncio task
    and tracks max concurrency to assert ≤1; `reap` awaits the task."""

    def __init__(self, worker=_passthrough_worker):
        self._worker = worker
        self.count = 0
        self.live = 0
        self.max_live = 0

    async def spawn(self, socket_path, turn_id, prompt):
        self.count += 1
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        return asyncio.create_task(self._worker(socket_path, turn_id, prompt))

    async def reap(self, handle):
        try:
            await handle
        finally:
            self.live -= 1


# --- harness: start all five actors in-process on one socket ---


class Harness:
    def __init__(self, core, tasks, source, outbound, renderer, spawns):
        self.core = core
        self.tasks = tasks
        self.source = source
        self.outbound = outbound
        self.renderer = renderer
        self.spawns = spawns

    async def teardown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.core.bus.stop()


async def _await(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def build_harness(sock_path, *, provider=None, spawns, turn_timeout=5.0, chain=None):
    # `provider=` (single fake) and `chain=` (a real multi-element provider chain,
    # AC1) are mutually exclusive — exactly one must be given.
    if provider is None and chain is None:
        raise ValueError("build_harness: pass one of provider= or chain= (got neither)")
    if provider is not None and chain is not None:
        raise ValueError("build_harness: pass one of provider= or chain= (got both)")
    broker_chain = chain if chain is not None else [provider]

    fs = ForkServer(sock_path, spawn=spawns.spawn, reap=spawns.reap, manage_gc=False)
    await fs.preload()
    # The scheduler is parked far out: these tests count lifecycle face pushes (_seq),
    # and a mood-face push from the reflex job (Story 3.3/5.1) would perturb that. Parking
    # `scheduler_interval` makes the resident scheduler task sleep past the test window so
    # NO job fires (the background-emitter rule — see test_endurance_soak); reflex_interval
    # is parked too so any future fast tick still wouldn't drift the mood face.
    core = Core(
        sock_path, fs, turn_timeout=turn_timeout, reflex_interval=3600, scheduler_interval=3600
    )

    source = _Source()
    outbound: list[str] = []
    renderer = StubRenderer()

    async def sink(text: str) -> None:
        outbound.append(text)

    tasks = [
        asyncio.create_task(core.run()),
        asyncio.create_task(run_broker(sock_path, broker_chain)),
        asyncio.create_task(run_display(sock_path, renderer)),
        asyncio.create_task(run_cli_transport(sock_path, inbound=source, outbound=sink)),
    ]

    # Bus must be up, and every receiver-side actor registered, before core emits to
    # them (an unregistered destination drops the envelope).
    await _await(lambda: core.bus._server is not None)
    await _await(
        lambda: all(
            core.bus._registry.get(a) is not None
            for a in (Actor.BROKER, Actor.DISPLAY, Actor.CHAT_TRANSPORT)
        )
    )
    return Harness(core, tasks, source, outbound, renderer, spawns)


# --- Task 5: AC1 — full turn round-trip ---


async def test_ac1_full_turn_round_trip(sock_path):
    spawns = Spawns()
    h = await build_harness(sock_path, provider=OkProvider(), spawns=spawns)
    try:
        h.source.feed("hello pet")

        # (a) the reply reaches the CLI outbound sink
        await _await(lambda: h.outbound == ["reply to: hello pet"])
        # (b) the display rendered at least one face snapshot (face reacted)
        await _await(lambda: len(h.renderer.rendered) >= 1)
        # (c) exactly one worker was spawned, never two concurrent
        assert spawns.count == 1
        assert spawns.max_live == 1
    finally:
        await h.teardown()


# --- Story 5.1 AC3: incoming messages bypass the scheduler (immediate, not gated) ---


async def test_inbound_message_bypasses_a_parked_scheduler(sock_path):
    """AD-14: 'Incoming messages/events bypass the scheduler entirely.' build_harness
    parks the scheduler at 3600s, so its loop won't tick for an hour. A turn that still
    completes within the normal poll window proves the inbox consumer is a PARALLEL path,
    not gated behind a scheduler tick — the scheduler is a sibling driver, not a queue in
    front of `core_inbox`."""
    spawns = Spawns()
    h = await build_harness(sock_path, provider=OkProvider(), spawns=spawns)
    try:
        assert h.core.scheduler_interval == 3600  # the scheduler is parked an hour out
        h.source.feed("urgent")
        await _await(lambda: h.outbound == ["reply to: urgent"])  # handled at once, not after a tick
        assert spawns.count == 1
    finally:
        await h.teardown()


# --- Task 6: AC2 — coalesce, never drop, never double-spawn ---


async def test_ac2_coalesce_never_drop_never_double_spawn(sock_path):
    provider = GatedProvider()
    spawns = Spawns()
    h = await build_harness(sock_path, provider=provider, spawns=spawns)
    try:
        # A starts a turn and blocks in the provider (turn in flight).
        h.source.feed("A")
        await _await(lambda: provider.entered == 1)
        assert spawns.count == 1  # exactly one turn so far

        # B arrives mid-turn: it must NOT spawn a second worker — it coalesces.
        h.source.feed("B")
        await asyncio.sleep(0.1)  # give a (wrong) second spawn a chance to happen
        assert spawns.count == 1
        assert spawns.max_live == 1

        # Release the gate: A completes, then exactly ONE catch-up turn folds B in.
        provider.gate.set()
        await _await(lambda: h.outbound == ["reply to: A", "reply to: B"])
        assert spawns.count == 2       # exactly two turns total
        assert spawns.max_live == 1    # never two workers at once
    finally:
        await h.teardown()


# --- Task 7: AC3 — graceful degrade, never hang ---


async def test_ac3_degrade_on_failure_result(sock_path):
    """Broker exhausts its single retry on a transient error -> Result(ok=False);
    core surfaces the graceful 'can't think' reply + an error face, no hang."""
    spawns = Spawns()
    h = await build_harness(sock_path, provider=AlwaysTransientProvider(), spawns=spawns)
    try:
        h.source.feed("hello?")
        await _await(lambda: h.outbound == [DEGRADE_TEXT])
        await _await(
            lambda: any(s.face == FACE_DEGRADED for s in h.renderer.rendered)
        )
    finally:
        await h.teardown()


async def test_ac3_turn_timeout_no_hang_and_late_result_discarded(sock_path):
    """A worker that sends its Job only AFTER the turn timeout: core degrades within
    the timeout (no hang), closes the turn, and the late Result is discarded by the
    fence (AD-12) — no double-delivery, no second turn from the stale Result."""

    async def late_worker(socket_path, turn_id, prompt):
        await asyncio.sleep(0.6)  # longer than the turn timeout below
        await run_worker(socket_path, turn_id, prompt, assemble=lambda m: m)

    spawns = Spawns(worker=late_worker)
    h = await build_harness(
        sock_path, provider=OkProvider(), spawns=spawns, turn_timeout=0.2
    )
    try:
        h.source.feed("are you there?")

        # Degrades within the timeout rather than hanging.
        await _await(lambda: h.outbound == [DEGRADE_TEXT], timeout=1.0)

        # The late Result eventually arrives (worker sleeps 0.6s) but is fenced out:
        # the outbound sink still holds ONLY the degrade reply — no second delivery,
        # no catch-up turn from the stale Result.
        await asyncio.sleep(0.8)
        assert h.outbound == [DEGRADE_TEXT]
        assert spawns.count == 1  # no second turn spawned from the stale Result
    finally:
        await h.teardown()


# --- Story 2.3: AC1 — whole-CHAIN exhaustion degrades end-to-end ---


async def test_ac1_whole_chain_exhaustion_degrades(sock_path):
    """A real 2-provider chain where BOTH providers fail: the broker iterates both,
    exhausts the chain (Story 2.2), returns the terminal failure Result, and core
    degrades — proving AC1's literal 'whole chain' (not just a single provider)."""
    spawns = Spawns()
    h = await build_harness(
        sock_path,
        chain=[AlwaysTransientProvider(), AlwaysTransientProvider()],
        spawns=spawns,
    )
    try:
        h.source.feed("hello?")
        await _await(lambda: h.outbound == [DEGRADE_TEXT])
        await _await(
            lambda: any(s.face == FACE_DEGRADED for s in h.renderer.rendered)
        )
        # Process stays alive: no latch, ready for the next turn.
        await _await(lambda: h.core.arbiter.is_idle)
        assert h.core.fence.is_idle
    finally:
        await h.teardown()


# --- Story 2.3: AC3 — auto-recovery (no latched degraded mode) ---


async def test_ac3_auto_recovers_when_provider_returns(sock_path):
    """Degrade during an outage, then resume normal turns the instant the provider
    answers again — no latched 'degraded mode', because each turn independently
    re-attempts the chain."""
    provider = RecoverableProvider()
    spawns = Spawns()
    h = await build_harness(sock_path, provider=provider, spawns=spawns)
    try:
        # Outage: the turn degrades.
        h.source.feed("are you there?")
        await _await(lambda: h.outbound == [DEGRADE_TEXT])

        # Provider comes back; the NEXT turn completes normally with model text.
        provider.down = False
        h.source.feed("you back?")
        await _await(
            lambda: h.outbound == [DEGRADE_TEXT, "reply to: you back?"]
        )
        assert spawns.count == 2  # two real turns ran; nothing latched
    finally:
        await h.teardown()


# --- Story 2.3: AC2 — offline acknowledges promptly (no hang) ---


async def test_ac2_offline_acknowledges_without_hanging(sock_path):
    """Fully offline (every provider raises) with a LONG turn timeout: the degrade
    ack must arrive fast — from the chain's fast failure Result, NOT by waiting out
    the turn timeout. A bounded 2s wait against a 30s timeout proves 'no hang'."""
    spawns = Spawns()
    h = await build_harness(
        sock_path,
        chain=[AlwaysTransientProvider(), AlwaysTransientProvider()],
        spawns=spawns,
        turn_timeout=30.0,
    )
    try:
        h.source.feed("anyone home?")
        # Well under the 30s turn timeout — the failure Result drives degrade.
        # 5s gives slack for a loaded CI host while still proving 6× headroom vs
        # the timeout (degrade came from the failure path, not the timeout).
        await _await(lambda: h.outbound == [DEGRADE_TEXT], timeout=5.0)
        # The slot is released after degrade (arbiter.complete() ran on the
        # failure path) — a stuck-True slot would coalesce every later turn.
        await _await(lambda: h.core.arbiter.is_idle)
    finally:
        await h.teardown()


# --- Story 4.4: CAP-6 — a fact from an earlier turn reaches a later prompt ---


async def test_cap6_fact_from_earlier_turn_reaches_later_prompt(sock_path):
    """The headline of Epic 4: a fact stated in an earlier turn is present in a LATER
    turn's assembled prompt (via the recent window / FTS5 recall), proving memory shapes
    the turn. Uses the REAL assembler (`worker=run_worker`) and a recording provider, and
    asserts on the PROMPT the broker received — never on a model's (non-deterministic)
    wording. Memory roots default to the conftest-redirected tmp `$HOME`."""
    # A distinctive token that does NOT appear in SYSTEM_INSTRUCTION's example — else the
    # assertion would pass on the static instruction alone, never actually testing recall.
    fact = "Cassandra-x9f3"
    assert fact not in SYSTEM_INSTRUCTION  # guard the guard

    provider = RecordingProvider()
    spawns = Spawns(worker=run_worker)  # real prompt assembly (default build_prompt)
    h = await build_harness(sock_path, provider=provider, spawns=spawns)
    try:
        h.source.feed(f"my preferred datastore is {fact}")
        # Turn 1 fully handled (reply delivered ⇒ core.record_turn has committed it).
        await _await(lambda: len(h.outbound) >= 1)

        h.source.feed("what is my preferred datastore?")
        await _await(lambda: len(provider.seen) >= 2)

        assembled = provider.seen[-1]
        # Strip the static system instruction so the fact can ONLY come from recall/recent.
        body = assembled.replace(SYSTEM_INSTRUCTION, "")
        assert fact in body  # the earlier fact reached the later prompt via memory
        assert "what is my preferred datastore?" in body  # current message present (last)
    finally:
        await h.teardown()
