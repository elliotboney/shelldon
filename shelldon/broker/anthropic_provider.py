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

#: Native-Claude default model; GLM (Z.ai) is selected via the chain's `glm` preset.
_DEFAULT_MODEL = "claude-sonnet-4-6"


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
