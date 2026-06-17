"""Config-driven ordered provider chain (AD-2).

The broker reads an ordered, comma-separated preset list from `PROVIDER_CHAIN`
(default `"glm"`) and constructs the providers in order — GLM first, alternates
after. Reordering or extending the chain is a **config line, never code** (AC2):
most providers are OpenAI-compatible, so a new one is one row in `_OPENAI_COMPAT`.
Credentials resolve **here, only from the broker's environment**, and are handed to
the pure config-in adapters — never placed on a Job/Result/Envelope (AD-2).

Story 2.1 builds + orders the chain; the broker executes the **primary** only. The
automatic fallback that advances through this list is Story 2.2.
"""

import os

from shelldon.broker.anthropic_provider import AnthropicProvider
from shelldon.broker.openai_provider import OpenAIProvider
from shelldon.broker.provider import LLMProvider

_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"


def _glm(env) -> LLMProvider:
    return AnthropicProvider(
        api_key=env.get("GLM_API_KEY") or env.get("ANTHROPIC_API_KEY"),
        base_url=env.get("GLM_BASE_URL") or env.get("ANTHROPIC_BASE_URL") or _ZAI_BASE_URL,
        model=env.get("GLM_MODEL") or env.get("ANTHROPIC_MODEL") or "glm-4.7",
    )


def _claude(env) -> LLMProvider:
    return AnthropicProvider(
        api_key=env.get("ANTHROPIC_API_KEY"),
        base_url=None,  # SDK default = native Anthropic
        model=env.get("CLAUDE_MODEL"),  # None → adapter's Claude default
    )


def _ollama(env) -> LLMProvider:
    base = env.get("OLLAMA_API_BASE")
    if not base:
        raise RuntimeError("OLLAMA_API_BASE is not set")
    stripped = base.rstrip("/")
    if not (stripped.endswith("/v1") or "/v1/" in stripped):
        stripped += "/v1"  # OpenAI-compatible path (idempotent: won't double-append)
    base = stripped
    return OpenAIProvider(
        api_key=env.get("OLLAMA_API_KEY") or "ollama",  # ignored by Ollama; SDK needs non-empty
        base_url=base,
        model=env.get("OLLAMA_MODEL"),
    )


#: OpenAI-compatible presets — name -> (default base_url, api-key env, model env).
#: Gemini fits here too (its OpenAI-compatible endpoint), so no separate SDK/adapter.
#: `base_url=None` → the openai SDK default (api.openai.com). Per-preset `{NAME}_BASE_URL`
#: overrides the default. A `name/...` model prefix (LiteLLM convention) is stripped.
_OPENAI_COMPAT = {
    "openai": (None, "OPENAI_API_KEY", "OPENAI_MODEL"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "CEREBRAS_MODEL"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "NVIDIA_MODEL"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "MISTRAL_MODEL"),
    "github": ("https://models.github.ai/inference", "GITHUB_TOKEN", "GITHUB_MODEL"),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
    ),
}


def _make_openai_compat(name, default_base, key_env, model_env):
    def build(env) -> LLMProvider:
        model = env.get(model_env)
        if model and model.startswith(f"{name}/"):
            model = model[len(name) + 1:]  # strip LiteLLM-style "name/" prefix
        return OpenAIProvider(
            api_key=env.get(key_env),
            base_url=env.get(f"{name.upper()}_BASE_URL") or default_base,
            model=model,
        )

    return build


#: preset name -> builder. Data-driven so adding/reordering is config, not code.
_PRESETS = {"glm": _glm, "claude": _claude, "ollama": _ollama}
_PRESETS.update({n: _make_openai_compat(n, *cfg) for n, cfg in _OPENAI_COMPAT.items()})


def build_chain(env=None) -> list[LLMProvider]:
    """Build the ordered provider chain from `PROVIDER_CHAIN` (default `"glm"`).

    Fails fast on an unknown preset or a preset whose required credential/config is
    missing — a misconfigured chain must not start silently degraded.
    """
    env = os.environ if env is None else env
    names = [n.strip().lower() for n in env.get("PROVIDER_CHAIN", "glm").split(",") if n.strip()]
    if not names:
        raise RuntimeError("PROVIDER_CHAIN is empty")
    chain: list[LLMProvider] = []
    for name in names:
        builder = _PRESETS.get(name)
        if builder is None:
            raise RuntimeError(f"unknown provider preset {name!r}; known: {sorted(_PRESETS)}")
        try:
            chain.append(builder(env))
        except RuntimeError as exc:
            raise RuntimeError(f"provider preset {name!r}: {exc}") from exc
    return chain
