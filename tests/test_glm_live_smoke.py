"""Live smoke (Epic 1 retro action #1): ONE real GLM call before Epic 2 builds on it.

De-risks the first real network path — credential, SDK, Z.ai Anthropic-compatible
endpoint, and response parsing — exactly once. Network-gated: skipped unless a key
env var is set (mirrors the Linux-gated real-fork tests). Run it deliberately with:

    set -a; . ./.env; set +a            # load ANTHROPIC_API_KEY (a Z.ai key)
    export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
    export ANTHROPIC_MODEL=glm-4.7
    uv run pytest -m live -s

The credential resolves ONLY from the broker's environment (AD-2) — never on the bus.
Accepts GLM_* names (the provider's own) or ANTHROPIC_* names (the .env convention).
"""

import os

import pytest

from shelldon.broker.glm import GLMProvider

_API_KEY = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _API_KEY,
        reason="no GLM_API_KEY/ANTHROPIC_API_KEY — live provider smoke skipped",
    ),
]


async def test_real_glm_call_returns_text():
    """One real call returns non-empty text — the whole real path works end to end."""
    provider = GLMProvider(
        api_key=_API_KEY,
        # None falls back to the provider's own GLM_* env / Z.ai defaults.
        base_url=os.environ.get("GLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"),
        model=os.environ.get("GLM_MODEL") or os.environ.get("ANTHROPIC_MODEL"),
    )
    reply = await provider.complete("Reply with one short friendly word.")
    print(f"\n[glm live smoke] model={provider._model} reply={reply!r}")
    assert isinstance(reply, str) and reply.strip(), "expected non-empty text from a live call"
