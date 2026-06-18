"""OpenAI-compatible provider — one adapter for Ollama-LAN / OpenAI / OpenRouter (AD-2).

A different wire format from the Anthropic adapter; the only difference between the
three endpoints is `base_url`/`model`/`api_key`. Pure config-in adapter (the chain
builder resolves env, so the credential never reaches a Job/Result/Envelope, AD-2).

Errors are mapped to the two `provider` exception types (never a raw SDK exception),
exactly like the Anthropic adapter, so the retry (1.4) and the fallback chain (Story
2.2) can key on them uniformly.
"""

import openai

from shelldon.broker.provider import PermanentProviderError, TransientProviderError


class OpenAIProvider:
    """An `LLMProvider` backed by the `openai` SDK (base_url selects the endpoint)."""

    def __init__(self, *, api_key, base_url=None, model, max_tokens=1024):
        # Ollama ignores the key but the SDK still requires a non-empty one — the
        # chain passes a placeholder for Ollama.
        if not api_key:
            raise RuntimeError("OpenAIProvider requires an api_key (broker-resolved)")
        if not model:
            raise RuntimeError("OpenAIProvider requires a model")
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
