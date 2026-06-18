"""Config-driven ordered chain: order follows PROVIDER_CHAIN; misconfig fails fast."""

import pytest

from shelldon.broker.anthropic_provider import AnthropicProvider
from shelldon.broker.chain import build_chain
from shelldon.broker.openai_provider import OpenAIProvider

_FULL_ENV = {
    "GLM_API_KEY": "sk-glm",
    "OLLAMA_API_BASE": "http://192.168.0.25:11434",
    "OLLAMA_MODEL": "gemma4:26b",
}


def test_default_chain_is_glm_only():
    chain = build_chain(env={"GLM_API_KEY": "sk-glm"})
    assert [type(p) for p in chain] == [AnthropicProvider]


def test_ordered_chain_built_in_config_order():
    chain = build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "glm,ollama"})
    assert [type(p) for p in chain] == [AnthropicProvider, OpenAIProvider]


def test_reordering_config_reorders_chain():
    chain = build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "ollama,glm"})
    assert [type(p) for p in chain] == [OpenAIProvider, AnthropicProvider]


def test_unknown_preset_raises():
    with pytest.raises(RuntimeError, match="unknown provider preset"):
        build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "glm,bogus"})


def test_missing_credential_raises_at_build_time():
    with pytest.raises(RuntimeError, match="glm"):
        build_chain(env={"PROVIDER_CHAIN": "glm"})  # no GLM/ANTHROPIC key


def test_missing_ollama_base_raises():
    with pytest.raises(RuntimeError, match="ollama"):
        build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "ollama", "OLLAMA_API_BASE": ""})


def test_ollama_base_gets_v1_suffix():
    chain = build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "ollama"})
    assert chain[0]._client.base_url.path.rstrip("/").endswith("/v1")


def test_gemini_preset_strips_litellm_prefix():
    chain = build_chain(
        env={"PROVIDER_CHAIN": "gemini", "GEMINI_API_KEY": "k", "GEMINI_MODEL": "gemini/gemini-2.5-flash"}
    )
    assert [type(p) for p in chain] == [OpenAIProvider]  # Gemini via its OpenAI-compatible endpoint
    assert chain[0]._model == "gemini-2.5-flash"  # "gemini/" prefix stripped


def test_three_wire_formats_chain_in_order():
    chain = build_chain(
        env={**_FULL_ENV, "GEMINI_API_KEY": "k", "GEMINI_MODEL": "gemini-2.5-flash",
             "PROVIDER_CHAIN": "glm,gemini,ollama"}
    )
    # glm=Anthropic-format, gemini=OpenAI-compatible, ollama=OpenAI-compatible.
    assert [type(p) for p in chain] == [AnthropicProvider, OpenAIProvider, OpenAIProvider]


@pytest.mark.parametrize("name", ["groq", "cerebras", "nvidia", "mistral", "github", "openai", "openrouter"])
def test_openai_compatible_presets_build(name):
    key_env = "GITHUB_TOKEN" if name == "github" else f"{name.upper()}_API_KEY"
    chain = build_chain(env={"PROVIDER_CHAIN": name, key_env: "k", f"{name.upper()}_MODEL": "m"})
    assert [type(p) for p in chain] == [OpenAIProvider]


def test_litellm_prefix_stripped_for_any_preset():
    chain = build_chain(env={"PROVIDER_CHAIN": "groq", "GROQ_API_KEY": "k", "GROQ_MODEL": "groq/llama-3.3-70b"})
    assert chain[0]._model == "llama-3.3-70b"


def test_claude_preset_builds_with_key():
    chain = build_chain(env={"PROVIDER_CHAIN": "claude", "ANTHROPIC_API_KEY": "sk-ant"})
    assert [type(p) for p in chain] == [AnthropicProvider]


def test_claude_preset_missing_key_raises():
    with pytest.raises(RuntimeError, match="claude"):
        build_chain(env={"PROVIDER_CHAIN": "claude"})  # no ANTHROPIC_API_KEY


def test_preset_names_are_case_insensitive():
    chain = build_chain(env={"PROVIDER_CHAIN": "GLM, Ollama", **_FULL_ENV})
    assert [type(p) for p in chain] == [AnthropicProvider, OpenAIProvider]


def test_ollama_base_with_v1_midpath_not_double_appended():
    chain = build_chain(
        env={"PROVIDER_CHAIN": "ollama", "OLLAMA_MODEL": "m", "OLLAMA_API_BASE": "http://host/v1/custom"}
    )
    assert "/v1/custom/v1" not in str(chain[0]._client.base_url)


def test_built_providers_are_named_by_preset():
    """Story 2.2 AC3: each provider carries its preset name for the audit record."""
    chain = build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "glm,ollama"})
    assert [p.name for p in chain] == ["glm", "ollama"]


def test_duplicate_presets_deduped_preserving_order():
    """Story 2.2 Task 7: a duplicate preset wastes a fallback slot — drop it."""
    chain = build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "glm,ollama,glm"})
    assert [p.name for p in chain] == ["glm", "ollama"]


def test_blank_chain_entries_dropped_with_warning(caplog):
    """Review #4: blank entries (stray/trailing comma) are dropped AND warned."""
    import logging

    with caplog.at_level(logging.WARNING, logger="shelldon.broker"):
        chain = build_chain(env={**_FULL_ENV, "PROVIDER_CHAIN": "glm,,ollama"})
    assert [p.name for p in chain] == ["glm", "ollama"]
    assert any("blank" in r.getMessage() for r in caplog.records)
