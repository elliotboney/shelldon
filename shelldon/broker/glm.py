"""GLM provider — Anthropic-format adapter against the Z.ai endpoint (AD-2, NFR5).

The first adapter built (Epic 2 generalizes it into an ordered chain). The
credential and endpoint resolve **only here, from the broker's environment** —
never hardcoded, never placed on a Job/Result/Envelope. The exact model id and
base URL are config, not a spine invariant.
"""

import os

import anthropic

from shelldon.broker.provider import PermanentProviderError, TransientProviderError

#: Z.ai's Anthropic-compatible endpoint (overridable via GLM_BASE_URL).
_DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
#: Exact GLM model id is config (GLM_MODEL); this is only a fallback default.
_DEFAULT_MODEL = "glm-4.6"


class GLMProvider:
    """An `LLMProvider` backed by the `anthropic` SDK pointed at Z.ai."""

    def __init__(self, *, api_key=None, base_url=None, model=None, max_tokens=1024):
        api_key = api_key or os.environ.get("GLM_API_KEY")
        if not api_key:
            raise RuntimeError("GLM_API_KEY is not set — the broker needs the model credential")
        self._model = model or os.environ.get("GLM_MODEL", _DEFAULT_MODEL)
        self._max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url or os.environ.get("GLM_BASE_URL", _DEFAULT_BASE_URL),
        )

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
            raise TransientProviderError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientProviderError(str(exc)) from exc
            raise PermanentProviderError(f"status {exc.status_code}") from exc
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        if not text:
            # No usable text (tool-use-only, max-tokens-with-no-text, or refusal) —
            # a no-text reply is a failed turn, not a silent empty success.
            raise PermanentProviderError("provider returned no text")
        return text
