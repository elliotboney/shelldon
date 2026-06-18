"""Story 2.2 AC4: fallback holds under sustained fault injection.

Drives `handle_job_chain` through a long run of turns with a deterministic,
intermittent failure pattern across the chain — some turns answer via the primary,
some fall through to the fallback, some exhaust the chain and return a terminal
failure Result. Asserts the three AC4 properties: no exception escapes (no crash),
every turn produces a Result within the loop (no hang), and the Python heap stays
flat over the run (no memory growth). Mirrors the NFR2 soak's tracemalloc method.

Extend manually with e.g. `SHELLDON_FALLBACK_SOAK_TURNS=5000 uv run pytest -m soak`.
"""

import gc
import logging
import os
import tracemalloc

import pytest

from shelldon.broker.broker import handle_job_chain
from shelldon.broker.provider import TransientProviderError
from shelldon.contracts import Job, Result

SOAK_TURNS = int(os.environ.get("SHELLDON_FALLBACK_SOAK_TURNS", "300"))

#: Steady-state Python-heap growth ceiling (bytes), measured by tracemalloc after a
#: warmup. handle_job_chain retains nothing per turn, so a breach means accumulation.
HEAP_BOUND = 250_000  # 250 KB (same headroom as the NFR2 soak)


class _Flaky:
    """Fails (transient) on turns where `fail_when(i)` is true, else returns text.

    The turn index travels in the Job payload so the pattern is deterministic and
    needs no per-call counter (Math.random is unavailable in this codebase anyway).
    """

    def __init__(self, name, fail_when):
        self.name = name
        self._fail_when = fail_when

    async def complete(self, prompt):
        i = int(prompt)
        if self._fail_when(i):
            raise TransientProviderError(f"{self.name} down @ {i}")
        return f"{self.name}:{i}"


@pytest.mark.soak
async def test_fallback_holds_under_sustained_faults():
    # primary fails every 2nd turn; fallback fails every 5th — so turns split into
    # primary-success, fallback-success, and whole-chain-exhausted (i % 10 == 0).
    primary = _Flaky("glm", lambda i: i % 2 == 0)
    fallback = _Flaky("ollama", lambda i: i % 5 == 0)
    chain = [primary, fallback]

    warmup = min(20, SOAK_TURNS // 5)
    via_primary = via_fallback = exhausted = 0

    # Silence the broker's per-turn fallback warnings during the run: pytest's log
    # capture retains every record O(N), which would swamp the heap signal we care
    # about (the chain's own retention). Same isolation the NFR2 soak does by clearing
    # its test-side sinks — production logging doesn't retain unboundedly.
    broker_log = logging.getLogger("shelldon.broker")
    saved_level = broker_log.level
    broker_log.setLevel(logging.ERROR)

    async def one_turn(i: int) -> Result:
        res = await handle_job_chain(Job(payload=str(i)), chain)
        assert isinstance(res, Result)  # always a Result — never an exception (no crash)
        return res

    # Warmup absorbs one-time lazy allocations before measuring.
    for i in range(warmup):
        await one_turn(i)

    gc.collect()
    tracemalloc.start()
    samples = [tracemalloc.get_traced_memory()[0]]
    try:
        for i in range(warmup, SOAK_TURNS):
            res = await one_turn(i)
            if res.ok and res.payload.startswith("glm:"):
                via_primary += 1
            elif res.ok and res.payload.startswith("ollama:"):
                via_fallback += 1
            else:
                exhausted += 1
            if i % 50 == 0:
                samples.append(tracemalloc.get_traced_memory()[0])
        gc.collect()
        samples.append(tracemalloc.get_traced_memory()[0])
        heap_delta = samples[-1] - samples[0]
    finally:
        tracemalloc.stop()
        broker_log.setLevel(saved_level)

    print(f"\n[fallback soak] turns={SOAK_TURNS} primary={via_primary} "
          f"fallback={via_fallback} exhausted={exhausted} heap_delta={heap_delta}B")

    # All three turn outcomes were actually exercised under load (fallback is real).
    assert via_primary > 0 and via_fallback > 0 and exhausted > 0
    # No monotonic heap growth across the run (no per-turn accumulation in the chain).
    assert heap_delta < HEAP_BOUND, (
        f"heap grew {heap_delta} bytes over {SOAK_TURNS} turns (bound {HEAP_BOUND})"
    )
