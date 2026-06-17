"""AC3: the credential lives only inside the broker and never on the bus."""

import re

import anthropic
import httpx
import msgspec
import openai
import pytest

from shelldon.broker.anthropic_provider import AnthropicProvider
from shelldon.broker.broker import handle_job
from shelldon.broker.openai_provider import OpenAIProvider
from shelldon.contracts import Job, Result

_CRED = re.compile(r"token|key|secret|password|api_?key|authorization|credential", re.IGNORECASE)

#: A sentinel that must never escape the adapter into a bus-bound Result.error.
_SECRET = "sk-SECRET-do-not-leak"


def test_provider_requires_a_credential():
    """The provider can't be built without the key — no silent keyless calls."""
    with pytest.raises(RuntimeError):
        AnthropicProvider(api_key=None)


def test_wire_types_carry_no_credential_fields():
    """Creds never travel on the bus: neither Job nor Result has a cred-shaped field."""
    for struct in (Job, Result):
        for field in msgspec.structs.fields(struct):
            assert not _CRED.search(field.name), f"{struct.__name__}.{field.name} is cred-shaped"


def test_credential_not_exposed_on_provider_public_api():
    """A constructed provider keeps the key private (not a public attribute)."""
    p = AnthropicProvider(api_key="sk-fake-not-real")
    public = {name: getattr(p, name) for name in vars(p) if not name.startswith("_")}
    assert all("sk-fake-not-real" not in str(v) for v in public.values())


async def test_anthropic_sdk_error_text_does_not_leak_into_result(monkeypatch):
    """Resolves 1.4 deferral: a raw SDK error message (which could embed a key/header)
    must not reach the bus-bound Result.error — the adapter surfaces only the type."""
    p = AnthropicProvider(api_key="sk-fake", model="glm-4.7")

    async def _boom(**kwargs):
        raise anthropic.APIConnectionError(
            message=f"connect failed {_SECRET}", request=httpx.Request("POST", "https://x")
        )

    monkeypatch.setattr(p._client.messages, "create", _boom)
    result = await handle_job(Job(payload="hi"), p)
    assert result.ok is False
    assert _SECRET not in (result.error or "")


async def test_openai_sdk_error_text_does_not_leak_into_result(monkeypatch):
    """Same hygiene guarantee for the OpenAI-compatible adapter."""
    p = OpenAIProvider(api_key="sk-fake", base_url="http://x/v1", model="m")

    async def _boom(**kwargs):
        raise openai.APIConnectionError(
            message=f"connect failed {_SECRET}", request=httpx.Request("POST", "http://x/v1")
        )

    monkeypatch.setattr(p._client.chat.completions, "create", _boom)
    result = await handle_job(Job(payload="hi"), p)
    assert result.ok is False
    assert _SECRET not in (result.error or "")
