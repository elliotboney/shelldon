"""Anthropic-format adapter: text returned, no-text is a failure, SDK errors mapped."""

import anthropic
import httpx
import pytest

from shelldon.broker.anthropic_provider import AnthropicProvider
from shelldon.broker.provider import PermanentProviderError, TransientProviderError


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, blocks):
        self.content = blocks


def _provider(monkeypatch, *, blocks=None, raises=None):
    p = AnthropicProvider(api_key="sk-fake", model="glm-4.7")

    async def _fake_create(**kwargs):
        if raises is not None:
            raise raises
        return _Resp(blocks)

    monkeypatch.setattr(p._client.messages, "create", _fake_create)
    return p


async def test_text_response_returned(monkeypatch):
    p = _provider(monkeypatch, blocks=[_Block("hello")])
    assert await p.complete("hi") == "hello"


async def test_empty_content_raises_permanent(monkeypatch):
    p = _provider(monkeypatch, blocks=[])
    with pytest.raises(PermanentProviderError):
        await p.complete("hi")


async def test_transient_sdk_error_mapped(monkeypatch):
    p = _provider(
        monkeypatch,
        raises=anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")),
    )
    with pytest.raises(TransientProviderError):
        await p.complete("hi")


def _status_error(status):
    req = httpx.Request("POST", "https://x")
    return anthropic.APIStatusError("err", response=httpx.Response(status, request=req), body=None)


async def test_status_5xx_is_transient(monkeypatch):
    p = _provider(monkeypatch, raises=_status_error(503))
    with pytest.raises(TransientProviderError):
        await p.complete("hi")


async def test_status_4xx_is_permanent(monkeypatch):
    p = _provider(monkeypatch, raises=_status_error(400))
    with pytest.raises(PermanentProviderError):
        await p.complete("hi")
