"""GLM provider edge: an empty completion is a failure, not silent success."""

import pytest

from shelldon.broker.glm import GLMProvider
from shelldon.broker.provider import PermanentProviderError


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, blocks):
        self.content = blocks


def _provider(monkeypatch, blocks):
    monkeypatch.setenv("GLM_API_KEY", "sk-fake")
    p = GLMProvider()

    async def _fake_create(**kwargs):
        return _Resp(blocks)

    monkeypatch.setattr(p._client.messages, "create", _fake_create)
    return p


async def test_text_response_returned(monkeypatch):
    p = _provider(monkeypatch, [_Block("hello")])
    assert await p.complete("hi") == "hello"


async def test_empty_content_raises_permanent(monkeypatch):
    p = _provider(monkeypatch, [])
    with pytest.raises(PermanentProviderError):
        await p.complete("hi")
