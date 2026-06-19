"""Live END-TO-END smokes (Epic 6 retro action #1) — does a REAL model, given the ACTUAL
assembled prompts/directives, reply AND emit well-formed ops that `parse_reply` decodes?

`test_provider_live_smoke.py` proves the provider *talks* (raw `complete` returns text).
THIS file proves the layer the whole memory/learning line depends on but that the synthetic
tests can't reach: that the real `SYSTEM_INSTRUCTION` + the real prompt assembly + the real
dream directive actually ELICIT `remember`/`resolve_learning`/`rewrite_summary` ops from a
live model — not just that the model is willing to talk.

Opt-in, network-gated (skips without a GLM/Anthropic key). Run deliberately with the broker
env loaded — these cost real tokens and are NOT in CI:

    set -a; . ./.env; set +a
    export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic ANTHROPIC_MODEL=glm-4.7
    uv run pytest -m live -s -k "live_turn or live_dream"

Read the printed `reply`/`ops` — a green run means the model emitted decodable ops; a red
run (or empty ops) IS the finding the retro asked us to surface (the prompt doesn't elicit
the behavior the mechanism assumes). Credentials resolve ONLY from the broker env (AD-2).
"""

import os

import pytest

from conftest import DummySpawner
from shelldon.broker.anthropic_provider import AnthropicProvider
from shelldon.contracts import Remember, ResolveLearning, RewriteSummary
from shelldon.core.runtime import Core
from shelldon.worker.prompt import SYSTEM_INSTRUCTION, assemble_prompt
from shelldon.worker.worker import parse_reply

pytestmark = pytest.mark.live

_GLM_KEY = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"


def _glm_provider() -> AnthropicProvider:
    return AnthropicProvider(
        api_key=_GLM_KEY,
        base_url=os.environ.get("GLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or _ZAI_BASE_URL,
        model=os.environ.get("GLM_MODEL") or os.environ.get("ANTHROPIC_MODEL"),
    )


@pytest.mark.skipif(not _GLM_KEY, reason="no GLM_API_KEY/ANTHROPIC_API_KEY")
async def test_live_turn_elicits_a_memory_op():
    """A real turn whose owner message strongly invites a fact-memory: the model should reply
    AND append a parseable ops block that decodes to a `remember`. Proves the hot-path memory
    mechanism works against a live model (not just the synthetic apply tests)."""
    provider = _glm_provider()
    prompt = assemble_prompt(
        "Please remember this for later: my favorite database is BigQuery.",
        about="I am shelldon, a small AI pet. I remember things my owner tells me.",
        system=SYSTEM_INSTRUCTION,
    )
    reply = await provider.complete(prompt)
    payload, ops = parse_reply(reply)
    print(f"\n[live turn] model={provider._model}\n--- reply ---\n{reply}\n--- parsed: payload={payload!r} ops={ops}")

    assert payload.strip(), "the model said nothing back"
    assert ops, "the model emitted NO parseable ops block — the prompt did not elicit a memory-op (FINDING)"
    assert any(isinstance(o, Remember) for o in ops), f"expected a `remember` op; got {[type(o).__name__ for o in ops]}"


@pytest.mark.skipif(not _GLM_KEY, reason="no GLM_API_KEY/ANTHROPIC_API_KEY")
async def test_live_dream_emits_resolve_and_summary(sock_path, tmp_path):
    """A real DREAM turn over the ACTUAL `_build_dream_prompt` directive (seeded with pending
    learnings): the model should classify the learnings and emit at least one `resolve_learning`
    op (promote/prune) — the dream's whole purpose. `rewrite_summary` is hoped-for, printed but
    not required. This is the single most-unverified behavior in the project."""
    core = Core(sock_path, DummySpawner(), checkpoint_path=tmp_path / "s.json")
    core.history.capture_learning("the owner ships features late at night", "night-owl", _now())
    core.history.capture_learning("the owner strongly prefers terse replies", "terse", _now())
    core.history.capture_learning("random one-off musing about the weather", "weather", _now())
    directive = core._build_dream_prompt()  # the REAL dream directive, learnings baked by id
    assert directive, "no pending learnings baked — seeding failed"

    prompt = assemble_prompt(
        directive,
        about="I am shelldon, a small AI pet that reflects on what it has noticed.",
        system=SYSTEM_INSTRUCTION,
    )
    reply = await _glm_provider().complete(prompt)
    payload, ops = parse_reply(reply)
    print(f"\n[live dream]\n--- directive ---\n{directive}\n--- reply ---\n{reply}\n--- parsed: payload={payload!r} ops={ops}")
    core.history.close()

    assert ops, "the model emitted NO parseable ops — the dream directive did not elicit classification (FINDING)"
    resolves = [o for o in ops if isinstance(o, ResolveLearning)]
    assert resolves, f"the dream emitted no `resolve_learning` op; got {[type(o).__name__ for o in ops]} (FINDING)"
    # a running summary is the AC3 nicety — observe it, don't gate on it
    if not any(isinstance(o, RewriteSummary) for o in ops):
        print("[live dream] note: no rewrite_summary emitted (AC3 consolidation skipped by the model this run)")


def _now():
    from datetime import UTC, datetime
    return datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
