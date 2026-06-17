"""OpenAI-compatible adapter: text returned, no-text is a failure, SDK errors mapped."""

import httpx
import openai
import pytest

from shelldon.broker.openai_provider import OpenAIProvider
from shelldon.broker.provider import PermanentProviderError, TransientProviderError


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content="hi", choices=None):
        self.choices = [_Choice(content)] if choices is None else choices


def _provider(monkeypatch, *, resp=None, raises=None):
    p = OpenAIProvider(api_key="sk-fake", base_url="http://x/v1", model="gemma4:26b")

    async def _fake_create(**kwargs):
        if raises is not None:
            raise raises
        return resp

    monkeypatch.setattr(p._client.chat.completions, "create", _fake_create)
    return p


async def test_text_response_returned(monkeypatch):
    p = _provider(monkeypatch, resp=_Resp("hello"))
    assert await p.complete("hi") == "hello"


async def test_empty_content_raises_permanent(monkeypatch):
    p = _provider(monkeypatch, resp=_Resp(content=None))
    with pytest.raises(PermanentProviderError):
        await p.complete("hi")


async def test_no_choices_raises_permanent(monkeypatch):
    p = _provider(monkeypatch, resp=_Resp(choices=[]))
    with pytest.raises(PermanentProviderError):
        await p.complete("hi")


async def test_transient_sdk_error_mapped(monkeypatch):
    p = _provider(
        monkeypatch,
        raises=openai.APITimeoutError(request=httpx.Request("POST", "http://x/v1")),
    )
    with pytest.raises(TransientProviderError):
        await p.complete("hi")


def _status_error(status):
    req = httpx.Request("POST", "http://x/v1")
    return openai.APIStatusError("err", response=httpx.Response(status, request=req), body=None)


async def test_status_5xx_is_transient(monkeypatch):
    p = _provider(monkeypatch, raises=_status_error(503))
    with pytest.raises(TransientProviderError):
        await p.complete("hi")


async def test_status_4xx_is_permanent(monkeypatch):
    p = _provider(monkeypatch, raises=_status_error(400))
    with pytest.raises(PermanentProviderError):
        await p.complete("hi")


def test_requires_api_key_and_model():
    with pytest.raises(RuntimeError):
        OpenAIProvider(api_key=None, model="m")
    with pytest.raises(RuntimeError):
        OpenAIProvider(api_key="k", model=None)
