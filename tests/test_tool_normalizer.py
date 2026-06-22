"""Story 9.1 (AC1): the broker normalizes each provider's NATIVE tool-call format into
the closed `ToolCall`/`Completion` contracts, so the worker loop stays provider-agnostic.

Pure unit tests over recorded SDK-shaped responses (SimpleNamespace mirrors the SDK's
attribute access) — no live client, no network. Covers Anthropic (also GLM via the
Anthropic-compatible endpoint) and OpenAI, plus the text-only no-tools case for each.
"""

import json
from types import SimpleNamespace

import pytest

from shelldon.broker.anthropic_provider import (
    _messages_to_anthropic,
    normalize_anthropic_response,
)
from shelldon.broker.openai_provider import normalize_openai_response
from shelldon.broker.provider import PermanentProviderError
from shelldon.contracts import Message, ToolCall


# --- Anthropic / GLM (Anthropic-compatible endpoint) ---


def _anthropic_resp(*blocks):
    return SimpleNamespace(content=list(blocks))


def test_anthropic_tool_use_normalizes_to_toolcall():
    resp = _anthropic_resp(
        SimpleNamespace(type="tool_use", id="toolu_1", name="get_time", input={"tz": "UTC"}),
    )
    comp = normalize_anthropic_response(resp)
    assert comp.ok and comp.payload == ""
    assert len(comp.tool_calls) == 1
    tc = comp.tool_calls[0]
    assert tc.id == "toolu_1" and tc.name == "get_time" and tc.args == {"tz": "UTC"}


def test_anthropic_text_and_tool_use_both_captured():
    """A reply with both a text block and a tool_use block keeps the text in payload AND
    the call in tool_calls (Anthropic can interleave) — GLM uses this exact shape."""
    resp = _anthropic_resp(
        SimpleNamespace(type="text", text="Let me check. "),
        SimpleNamespace(type="tool_use", id="toolu_2", name="get_time", input={}),
    )
    comp = normalize_anthropic_response(resp)
    assert comp.ok and comp.payload == "Let me check. "
    assert comp.tool_calls[0].name == "get_time" and comp.tool_calls[0].args == {}


def test_anthropic_text_only_has_no_tool_calls():
    resp = _anthropic_resp(SimpleNamespace(type="text", text="hello there"))
    comp = normalize_anthropic_response(resp)
    assert comp.ok and comp.payload == "hello there" and comp.tool_calls == ()


def test_anthropic_empty_reply_is_a_failed_turn():
    """Neither text nor tool calls → a failed turn (mirrors complete()'s no-text rule)."""
    with pytest.raises(PermanentProviderError):
        normalize_anthropic_response(_anthropic_resp())


def test_anthropic_none_input_normalizes_to_empty_args():
    """A no-arg tool call can arrive with input=None — must not raise (dict(None))."""
    resp = _anthropic_resp(SimpleNamespace(type="tool_use", id="t1", name="get_time", input=None))
    comp = normalize_anthropic_response(resp)
    assert comp.tool_calls[0].args == {}


def test_messages_to_anthropic_keeps_assistant_text_with_tool_use():
    """An assistant message with BOTH text and tool-calls replays the text block first —
    replaying tool_use without its leading text is an Anthropic 400."""
    msgs = [Message(role="assistant", content="Let me check.",
                    tool_calls=(ToolCall(id="t1", name="get_time", args={}),))]
    sdk = _messages_to_anthropic(msgs)
    blocks = sdk[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Let me check."}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["id"] == "t1"


def test_messages_to_anthropic_omits_empty_assistant_text():
    """No assistant text → only the tool_use block (no empty text block)."""
    msgs = [Message(role="assistant", tool_calls=(ToolCall(id="t1", name="get_time", args={}),))]
    blocks = _messages_to_anthropic(msgs)[0]["content"]
    assert len(blocks) == 1 and blocks[0]["type"] == "tool_use"


# --- OpenAI / OpenAI-compatible (Ollama / OpenRouter) ---


def _openai_resp(*, content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _openai_tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def test_openai_tool_calls_parse_json_arguments_to_dict():
    resp = _openai_resp(
        tool_calls=[_openai_tool_call("call_1", "get_time", json.dumps({"tz": "UTC"}))]
    )
    comp = normalize_openai_response(resp)
    assert comp.ok and comp.payload == ""
    tc = comp.tool_calls[0]
    assert tc.id == "call_1" and tc.name == "get_time"
    assert tc.args == {"tz": "UTC"}  # the JSON STRING was parsed to a dict


def test_openai_text_only_has_no_tool_calls():
    resp = _openai_resp(content="just text")
    comp = normalize_openai_response(resp)
    assert comp.ok and comp.payload == "just text" and comp.tool_calls == ()


def test_openai_empty_reply_is_a_failed_turn():
    with pytest.raises(PermanentProviderError):
        normalize_openai_response(_openai_resp())


def test_openai_malformed_arguments_raise_permanent():
    """Malformed model-emitted JSON args are a permanent (bad-request-shaped) failure, not
    an opaque 'unexpected provider error' — handle_job maps PermanentProviderError cleanly."""
    resp = _openai_resp(tool_calls=[_openai_tool_call("call_1", "get_time", "{not json")])
    with pytest.raises(PermanentProviderError):
        normalize_openai_response(resp)
