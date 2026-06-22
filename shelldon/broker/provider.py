"""The provider seam — the broker's SDK-agnostic view of an LLM (AD-2).

The broker's retry logic keys on these exception types, never on a vendor SDK's
exceptions, so broker logic stays testable with fakes and SDK-free. A concrete
provider (e.g. `glm.GLMProvider`) translates its SDK's errors into these.
"""

from typing import Protocol, runtime_checkable

from shelldon.contracts import Completion, Message, ToolDefinition


@runtime_checkable
class LLMProvider(Protocol):
    #: Audit label (the chain preset name, e.g. "glm"/"ollama") — never a credential.
    #: The fallback chain records which provider answered by this name (Story 2.2).
    name: str

    async def complete(self, prompt: str) -> str:
        """Return the model's text completion for `prompt`."""
        ...

    async def complete_with_tools(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> Completion:
        """Native function-calling round-trip (Story 9.1). Send the running `messages`
        and the available `tools`, normalize the provider's native tool-use format into
        closed `ToolCall` contracts on the returned `Completion`. Separate from
        `complete()` so the text-only path stays untouched. On an SDK error this RAISES
        `TransientProviderError`/`PermanentProviderError` (same taxonomy as `complete`),
        so the broker's shared retry/fallback keys on them; the broker is what maps an
        exhausted failure into `Completion(ok=False, error=...)`."""
        ...


class TransientProviderError(Exception):
    """A retryable failure — timeout, rate-limit, or a 5xx from the provider."""


class PermanentProviderError(Exception):
    """A non-retryable failure — bad request, auth, or any other 4xx."""
