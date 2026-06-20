"""Story 8.0 — FULL-STACK live smokes (Epic 6 retro action #1, the dominant risk).

`test_turn_dream_live_smoke.py` proves the prompt *elicits* a decodable op (it calls
`provider.complete(assemble_prompt(...))` directly). THIS file proves the layer it bypasses:
that a real owner turn / a real dream, driven through the ACTUAL wire — core admits the turn →
the worker (real `run_worker`, real `assemble_prompt`) calls the broker → the broker runs the
GLM chain → the `Result` returns over the bus → **core APPLIES the ops** — produces the
observable end state: a `facts/` file written, a learning row transitioned.

It reuses the Story 1.8 in-process harness (`build_harness`, with the REAL `chain=` and
`Spawns(worker=run_worker)`); the worker runs as an in-process task (the injected spawn seam),
so no real `os.fork()` is needed — the real prompt + real provider + real apply path are all
exercised. The conftest autouse fixture redirects the memory tree + history to `tmp_path`, so
the asserted `facts/` file / learning row live under the test's tmp dir, never real `$HOME`.

Opt-in, network-gated (skips without a GLM/Anthropic key), NOT in CI — these cost real tokens.
Credentials resolve ONLY from the broker env (AD-2). Run deliberately:

    set -a; . ./.env; set +a
    export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic GLM_MODEL=glm-4.7
    uv run pytest -m live -s -k full_stack

A green run = the live model's ops reached core and were applied end-to-end. A red run (degrade,
no `facts/` file, no learning transition) IS the finding the retro asked us to surface, captured
in `live-smoke-findings-{date}.md` (AC3).
"""

import os
from datetime import UTC, datetime

import pytest

from shelldon.broker.chain import build_chain
from shelldon.core.runtime import DEGRADE_TEXT, FACE_DEGRADED
from shelldon.worker.worker import run_worker
from test_end_to_end_turn import Spawns, _await, build_harness

pytestmark = pytest.mark.live

_GLM_KEY = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

#: Real network latency — well above the ~3s/call the elicitation smoke observed, so a slow
#: round-trip never degrades the turn under test (the point is the apply path, not timing).
_LIVE_TURN_TIMEOUT = 60.0


def _now() -> datetime:
    return datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


@pytest.mark.skipif(not _GLM_KEY, reason="no GLM_API_KEY/ANTHROPIC_API_KEY")
async def test_full_stack_live_turn_applies_a_memory_op(sock_path, tmp_path):
    """A real owner turn driven through the WHOLE wire against live GLM: the reply reaches the
    outbound sink, the face is non-degraded, AND core APPLIED the model's `remember` — a file
    lands under the curated `facts/` tree. Proves the hot-path memory mechanism end-to-end
    (not just that the prompt elicits the op — that's the elicitation smoke)."""
    h = await build_harness(
        sock_path,
        chain=build_chain(os.environ),
        spawns=Spawns(worker=run_worker),
        turn_timeout=_LIVE_TURN_TIMEOUT,
    )
    try:
        h.source.feed("Please remember this for later: my favorite database is BigQuery.")
        await _await(lambda: len(h.outbound) >= 1, timeout=_LIVE_TURN_TIMEOUT)
        reply = h.outbound[0]

        facts_dir = tmp_path / "memory" / "facts"
        files = sorted(facts_dir.glob("*.md")) if facts_dir.exists() else []
        contents = {f.name: f.read_text() for f in files}
        print(f"\n[full-stack turn]\n--- reply ---\n{reply}\n--- applied facts/ files ---\n{contents}")

        assert reply and reply != DEGRADE_TEXT, "the turn degraded — the live chain failed (FINDING)"
        # renderer.rendered is list[StateSnapshot]; compare the .face token, not the snapshot.
        assert not any(s.face == FACE_DEGRADED for s in h.renderer.rendered), "a degraded face was pushed (FINDING)"
        assert files, "no facts/ file written — core did not APPLY a `remember` end-to-end (FINDING)"
    finally:
        await h.teardown()


@pytest.mark.skipif(not _GLM_KEY, reason="no GLM_API_KEY/ANTHROPIC_API_KEY")
async def test_full_stack_live_dream_applies_resolve_learning(sock_path, tmp_path):
    """A real dream driven through the WHOLE wire: pending learnings seeded, the REAL
    `_build_dream_prompt` directive fed in, and on a green run core APPLIES the model's
    `resolve_learning` ops — a seeded `pending` learning is now `promoted`/`pruned` in sqlite.
    Asserts the soft status transition LANDED, not just that the op decoded. The single
    most-unverified behavior in the project, end-to-end."""
    h = await build_harness(
        sock_path,
        chain=build_chain(os.environ),
        spawns=Spawns(worker=run_worker),
        turn_timeout=_LIVE_TURN_TIMEOUT,
    )
    core = h.core
    core.history.capture_learning("the owner ships features late at night", "night-owl", _now())
    core.history.capture_learning("the owner strongly prefers terse replies", "terse", _now())
    core.history.capture_learning("random one-off musing about the weather", "weather", _now())
    before = len(core.history.pending_learnings())
    directive = core._build_dream_prompt()  # the REAL dream directive, learnings baked by id
    assert directive, "no pending learnings baked — seeding failed"

    try:
        h.source.feed(directive)  # drive the dream directive through the live wire as a turn
        await _await(lambda: len(h.outbound) >= 1, timeout=_LIVE_TURN_TIMEOUT)
        reply = h.outbound[0]
        after = core.history.pending_learnings()

        print(
            f"\n[full-stack dream]\n--- directive ---\n{directive}\n--- reply ---\n{reply}\n"
            f"--- pending learnings: {before} before -> {len(after)} after "
            f"(remaining: {[r['pattern_key'] for r in after]}) ---"
        )

        assert reply and reply != DEGRADE_TEXT, "the dream turn degraded — the live chain failed (FINDING)"
        assert len(after) < before, (
            "no learning transitioned — the dream's `resolve_learning` did not APPLY end-to-end (FINDING)"
        )
    finally:
        await h.teardown()
