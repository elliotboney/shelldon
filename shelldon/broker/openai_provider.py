"""OpenAI-compatible provider — one adapter for Ollama-LAN / OpenAI / OpenRouter (AD-2).

A different wire format from the Anthropic adapter; the only difference between the
three endpoints is `base_url`/`model`/`api_key`. Pure config-in adapter (the chain
builder resolves env, so the credential never reaches a Job/Result/Envelope, AD-2).

Errors are mapped to the two `provider` exception types (never a raw SDK exception),
exactly like the Anthropic adapter, so the retry (1.4) and the fallback chain (Story
2.2) can key on them uniformly.
"""

import json

import openai

from shelldon.broker.provider import PermanentProviderError, TransientProviderError
from shelldon.contracts import Completion, Message, ToolCall, ToolDefinition


def _tools_to_openai(tools: list[ToolDefinition]) -> list[dict]:
    """Project our `ToolDefinition`s to the OpenAI function-tools schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.params_schema,
            },
        }
        for t in tools
    ]


def _messages_to_openai(messages: list[Message]) -> list[dict]:
    """Convert our `Message` structs to the OpenAI chat message format. Assistant
    tool-calls become a message with `tool_calls` (arguments re-serialized to JSON);
    tool results become `role="tool"` messages keyed by `tool_call_id`."""
    sdk: list[dict] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            sdk.append({
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in m.tool_calls
                ],
            })
        elif m.role == "tool":
            sdk.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            sdk.append({"role": m.role, "content": m.content})
    return sdk


def normalize_openai_response(resp) -> Completion:
    """Normalize an OpenAI SDK response into a closed `Completion`. `message.content`
    becomes `payload`; each `message.tool_calls` entry becomes a `ToolCall` with its
    JSON-string `arguments` parsed to a dict. A reply with neither is a failed turn."""
    choice = resp.choices[0] if resp.choices else None
    message = getattr(choice, "message", None) if choice is not None else None
    text = (getattr(message, "content", None) or "") if message is not None else ""
    calls: list[ToolCall] = []
    for tc in (getattr(message, "tool_calls", None) or []) if message is not None else []:
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except json.JSONDecodeError as exc:
            # Malformed model-emitted arguments are a bad-request-shaped failure, not a
            # retryable transient one — raise the permanent type so handle_job maps it
            # cleanly instead of catching it as an opaque "unexpected provider error".
            raise PermanentProviderError(f"malformed tool arguments: {exc}") from exc
        calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
    if not text and not calls:
        raise PermanentProviderError("provider returned no text or tool calls")
    return Completion(ok=True, payload=text, tool_calls=tuple(calls))


class OpenAIProvider:
    """An `LLMProvider` backed by the `openai` SDK (base_url selects the endpoint)."""

    def __init__(self, *, api_key, base_url=None, model, max_tokens=1024, name="openai"):
        # Ollama ignores the key but the SDK still requires a non-empty one — the
        # chain passes a placeholder for Ollama.
        if not api_key:
            raise RuntimeError("OpenAIProvider requires an api_key (broker-resolved)")
        if not model:
            raise RuntimeError("OpenAIProvider requires a model")
        self.name = name  # audit label (chain preset name); never a credential
        self._model = model
        self._max_tokens = max_tokens
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(self, prompt: str) -> str:
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        ) as exc:
            # Surface only the SDK exception TYPE, never str(exc): the message can
            # carry request headers/keys and this text crosses the bus in Result.error
            # (AD-2). Full detail stays in the chained __cause__ for broker-side logs.
            raise TransientProviderError(type(exc).__name__) from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientProviderError(type(exc).__name__) from exc
            raise PermanentProviderError(f"status {exc.status_code}") from exc
        text = resp.choices[0].message.content if resp.choices else None
        if not text:
            # No usable text — a no-text reply is a failed turn, not silent success.
            raise PermanentProviderError("provider returned no text")
        return text

    async def complete_with_tools(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> Completion:
        """Native function-calling round-trip (Story 9.1). Raises the same provider
        exception types as `complete` on SDK errors so the broker's retry/fallback keys
        on them uniformly; returns a normalized `Completion` on success. OpenAI's
        `arguments` is a JSON string — `normalize_openai_response` parses it to a dict."""
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=_messages_to_openai(messages),
                tools=_tools_to_openai(tools),
            )
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        ) as exc:
            raise TransientProviderError(type(exc).__name__) from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientProviderError(type(exc).__name__) from exc
            raise PermanentProviderError(f"status {exc.status_code}") from exc
        return normalize_openai_response(resp)
