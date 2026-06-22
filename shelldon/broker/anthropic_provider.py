"""Anthropic-format provider — the first wire-format adapter (AD-2).

Serves **GLM-5.2 via Z.ai's Anthropic-compatible endpoint** AND **native Claude** —
the only difference is config (`base_url`/`model`/`api_key`), not code. The chain
builder (`chain.py`) resolves that config from the broker's environment and
constructs this adapter; the adapter itself is pure (config in, no env reads), so
the credential never appears on a Job/Result/Envelope (AD-2).

Errors are mapped to the two `provider` exception types (never a raw SDK exception)
so the retry (1.4) and the fallback chain (Story 2.2) can key on them uniformly.
"""

import anthropic

from shelldon.broker.provider import PermanentProviderError, TransientProviderError
from shelldon.contracts import Completion, Message, ToolCall, ToolDefinition

#: Native-Claude default model; GLM (Z.ai) is selected via the chain's `glm` preset.
_DEFAULT_MODEL = "claude-sonnet-4-6"


def _tools_to_anthropic(tools: list[ToolDefinition]) -> list[dict]:
    """Project our `ToolDefinition`s to the Anthropic tools schema (`input_schema`)."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.params_schema}
        for t in tools
    ]


def _messages_to_anthropic(messages: list[Message]) -> list[dict]:
    """Convert our `Message` structs to the Anthropic SDK message format. Assistant
    tool-calls become `tool_use` content blocks; tool results become a `user` message
    with a `tool_result` block (Anthropic carries tool results on the user turn)."""
    sdk: list[dict] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            # Keep any leading text alongside the tool_use blocks — Anthropic can return
            # mixed text+tool_use, and replaying tool_use without that text is a 400.
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks.extend(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                for tc in m.tool_calls
            )
            sdk.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            sdk.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                ],
            })
        else:
            sdk.append({"role": m.role, "content": m.content})
    return sdk


def normalize_anthropic_response(resp) -> Completion:
    """Normalize an Anthropic SDK response into a closed `Completion`. Text blocks join
    into `payload`; `tool_use` blocks become `ToolCall` contracts. A reply with neither
    text nor tool calls is a failed turn (mirrors `complete`'s no-text rule)."""
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            # `input` may be None for a no-arg tool call — `dict(None)` would TypeError.
            calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
    text = "".join(text_parts)
    if not text and not calls:
        raise PermanentProviderError("provider returned no text or tool calls")
    return Completion(ok=True, payload=text, tool_calls=tuple(calls))


class AnthropicProvider:
    """An `LLMProvider` backed by the `anthropic` SDK (native Claude or Z.ai/GLM)."""

    def __init__(self, *, api_key, base_url=None, model=None, max_tokens=1024, name="anthropic"):
        if not api_key:
            raise RuntimeError("AnthropicProvider requires an api_key (broker-resolved)")
        self.name = name  # audit label (chain preset name); never a credential
        self._model = model or _DEFAULT_MODEL
        self._max_tokens = max_tokens
        # base_url=None → the SDK's default (api.anthropic.com, native Claude); a
        # Z.ai base url makes the same adapter speak to GLM.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def complete(self, prompt: str) -> str:
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            # Surface only the SDK exception TYPE, never str(exc): the message can
            # carry request headers/keys and this text crosses the bus in Result.error
            # (AD-2). Full detail stays in the chained __cause__ for broker-side logs.
            raise TransientProviderError(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientProviderError(type(exc).__name__) from exc
            raise PermanentProviderError(f"status {exc.status_code}") from exc
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
        if not text:
            # No usable text (tool-use-only, max-tokens-with-no-text, or refusal) —
            # a no-text reply is a failed turn, not a silent empty success.
            raise PermanentProviderError("provider returned no text")
        return text

    async def complete_with_tools(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> Completion:
        """Native function-calling round-trip (Story 9.1). Raises the same provider
        exception types as `complete` on SDK errors so the broker's retry/fallback keys
        on them uniformly; returns a normalized `Completion` (text + `ToolCall`s) on
        success. GLM (Z.ai) uses this same Anthropic-compatible path unchanged."""
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=_messages_to_anthropic(messages),
                tools=_tools_to_anthropic(tools),
            )
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            raise TransientProviderError(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientProviderError(type(exc).__name__) from exc
            raise PermanentProviderError(f"status {exc.status_code}") from exc
        return normalize_anthropic_response(resp)
