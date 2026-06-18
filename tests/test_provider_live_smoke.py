"""Live provider smokes — one real call per wire format, before the chain (2.2) leans
on them. Network-gated per test: each skips unless its endpoint's env is set (mirrors
the Linux-gated real-fork tests). Run deliberately with the broker env loaded:

    set -a; . ./.env; set +a            # ANTHROPIC_API_KEY (Z.ai), OLLAMA_API_BASE, OLLAMA_MODEL
    export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic ANTHROPIC_MODEL=glm-4.7
    uv run pytest -m live -s

Credentials resolve ONLY from the broker's environment (AD-2) — never on the bus.
"""

import os

import pytest

from shelldon.broker.anthropic_provider import AnthropicProvider
from shelldon.broker.openai_provider import OpenAIProvider

pytestmark = pytest.mark.live

_GLM_KEY = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
_OLLAMA_BASE = os.environ.get("OLLAMA_API_BASE")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL")
_GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
_GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash").removeprefix("gemini/")
_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.mark.skipif(not _GLM_KEY, reason="no GLM_API_KEY/ANTHROPIC_API_KEY")
async def test_anthropic_format_live_returns_text():
    """Anthropic-format adapter (GLM via Z.ai): a real call returns non-empty text."""
    provider = AnthropicProvider(
        api_key=_GLM_KEY,
        base_url=os.environ.get("GLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or _ZAI_BASE_URL,
        model=os.environ.get("GLM_MODEL") or os.environ.get("ANTHROPIC_MODEL"),
    )
    reply = await provider.complete("Reply with one short friendly word.")
    print(f"\n[anthropic live] model={provider._model} reply={reply!r}")
    assert isinstance(reply, str) and reply.strip()


@pytest.mark.skipif(
    not (_OLLAMA_BASE and _OLLAMA_MODEL), reason="no OLLAMA_API_BASE/OLLAMA_MODEL"
)
async def test_openai_compatible_live_returns_text():
    """OpenAI-compatible adapter (Ollama-over-LAN): a real call returns non-empty text."""
    base = _OLLAMA_BASE.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    provider = OpenAIProvider(api_key="ollama", base_url=base, model=_OLLAMA_MODEL)
    reply = await provider.complete("Reply with one short friendly word.")
    print(f"\n[openai-compat live] model={provider._model} reply={reply!r}")
    assert isinstance(reply, str) and reply.strip()


@pytest.mark.skipif(not _GEMINI_KEY, reason="no GEMINI_API_KEY")
async def test_gemini_openai_compatible_live_returns_text():
    """Gemini via its OpenAI-compatible endpoint: a real call returns non-empty text."""
    provider = OpenAIProvider(api_key=_GEMINI_KEY, base_url=_GEMINI_OPENAI_BASE, model=_GEMINI_MODEL)
    reply = await provider.complete("Reply with one short friendly word.")
    print(f"\n[gemini openai-compat live] model={provider._model} reply={reply!r}")
    assert isinstance(reply, str) and reply.strip()
