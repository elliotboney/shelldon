"""The provider seam — the broker's SDK-agnostic view of an LLM (AD-2).

The broker's retry logic keys on these exception types, never on a vendor SDK's
exceptions, so broker logic stays testable with fakes and SDK-free. A concrete
provider (e.g. `glm.GLMProvider`) translates its SDK's errors into these.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(self, prompt: str) -> str:
        """Return the model's text completion for `prompt`."""
        ...


class TransientProviderError(Exception):
    """A retryable failure — timeout, rate-limit, or a 5xx from the provider."""


class PermanentProviderError(Exception):
    """A non-retryable failure — bad request, auth, or any other 4xx."""
