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


# --- Story 10.5 (AC2): per-turn prompt-cache signal logging ---


class _Usage:
    def __init__(self, *, created=None, read=None):
        self.cache_creation_input_tokens = created
        self.cache_read_input_tokens = read


async def test_cache_read_signal_logged(monkeypatch, caplog):
    """The cache-read signal is logged at INFO so the owner can SEE the byte-stable persona prefix
    caching — proven against a FAKED SDK response carrying usage.cache_read_input_tokens (no network)."""
    p = AnthropicProvider(api_key="sk-fake", model="claude-sonnet-4-6", name="claude")

    async def _fake_create(**kwargs):
        resp = _Resp([_Block("hello")])
        resp.usage = _Usage(created=0, read=2048)  # prefix hit: 2048 tokens read from cache
        return resp

    monkeypatch.setattr(p._client.messages, "create", _fake_create)
    with caplog.at_level("INFO", logger="shelldon.broker"):
        assert await p.complete("hi") == "hello"
    assert any("cache_read_input_tokens=2048" in r.message for r in caplog.records)


async def test_cache_signal_absent_does_not_crash_or_log(monkeypatch, caplog):
    """GLM/z.ai may omit the cache usage fields — getattr-guarded, so the turn succeeds and nothing
    cache-related is logged (no silent assumption that caching happened)."""
    p = AnthropicProvider(api_key="sk-fake", model="glm-4.7", name="glm")

    async def _fake_create(**kwargs):
        resp = _Resp([_Block("hi")])
        resp.usage = _Usage()  # both fields None (provider didn't surface them)
        return resp

    monkeypatch.setattr(p._client.messages, "create", _fake_create)
    with caplog.at_level("INFO", logger="shelldon.broker"):
        assert await p.complete("yo") == "hi"
    assert not any("cache usage" in r.message for r in caplog.records)
