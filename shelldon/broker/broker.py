"""Broker turn logic (AD-2): a Job becomes a Result.

The credential lives only inside the provider (see `chain.py`); it never appears on
the Job, the Result, or anywhere on the bus. A failed turn surfaces as a Result
error variant — never an exception across the bus (Consistency Conventions).

Two layers (AD-2 "provider chain WITH retry/fallback"):
  - `handle_job(job, provider)` — one provider, retrying a transient error once,
    with a small backoff so an immediate retry doesn't hammer a rate-limited endpoint.
  - `handle_job_chain(job, chain)` — iterates the ordered chain (Story 2.2): the
    first success wins, ANY failure advances to the next provider, and an exhausted
    chain returns the last failure Result (terminal failure → arbiter degrade, 2.3).
"""

import asyncio
import logging

from shelldon.broker.provider import (
    LLMProvider,
    PermanentProviderError,
    TransientProviderError,
)
from shelldon.contracts import Job, Result

log = logging.getLogger("shelldon.broker")

#: One retry after the first transient failure (AD-2 basic retry).
_MAX_ATTEMPTS = 2

#: Backoff before the in-provider transient retry (seconds). Bounded so the pet
#: doesn't immediately re-hit a rate-limited/flapping endpoint and burn its one
#: retry (1.4 deferral). Module-level so tests can set it to 0 — no real waits.
_RETRY_BACKOFF_S = 0.5


async def handle_job(job: Job, provider: LLMProvider) -> Result:
    """Call the provider for `job`, retrying once (after a backoff) on a transient error."""
    last_error: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = await provider.complete(job.payload)
            return Result(ok=True, payload=text)
        except TransientProviderError as exc:
            last_error = f"transient provider error: {exc}"
            log.warning("provider transient error (attempt %d/%d): %s", attempt, _MAX_ATTEMPTS, exc)
            if attempt < _MAX_ATTEMPTS:
                # A 0 backoff (tests) means no delay; a positive one pauses before the
                # retry so the single retry budget isn't wasted on a rate-limited endpoint.
                if _RETRY_BACKOFF_S:
                    await asyncio.sleep(_RETRY_BACKOFF_S)
        except PermanentProviderError as exc:
            return Result(ok=False, error=f"provider error: {exc}")
        except Exception as exc:
            # Anything the provider didn't classify must still surface as a Result,
            # never an exception across the bus. Unexpected == non-retryable.
            log.warning("unexpected provider error: %s", type(exc).__name__)
            return Result(ok=False, error=f"unexpected provider error: {type(exc).__name__}")
    return Result(ok=False, error=last_error or "provider unavailable")


async def handle_job_chain(job: Job, chain: list[LLMProvider]) -> Result:
    """Run `job` through the ordered chain, advancing on ANY failure (Story 2.2).

    Returns the first successful Result; on exhaustion returns the last provider's
    failure Result (the terminal failure the arbiter degrades on — Story 2.3). The
    winning provider is recorded for audit by its preset name (no credential, AD-2).
    """
    result = Result(ok=False, error="empty provider chain")
    for fallbacks, provider in enumerate(chain):
        result = await handle_job(job, provider)
        if result.ok:
            log.info("turn answered by provider %r (after %d fallback(s))", provider.name, fallbacks)
            return result
        log.warning("provider %r failed, advancing: %s", provider.name, result.error)
    return result
