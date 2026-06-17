"""Broker turn logic (AD-2): a Job becomes a Result, retrying a transient error once.

The credential lives only inside the provider (see `glm.py`); it never appears on
the Job, the Result, or anywhere on the bus. A failed turn surfaces as a Result
error variant — never an exception across the bus (Consistency Conventions). The
full multi-provider chain/fallback is Epic 2; here it's one provider + one retry.
"""

import logging

from shelldon.broker.provider import (
    LLMProvider,
    PermanentProviderError,
    TransientProviderError,
)
from shelldon.contracts import Job, Result

log = logging.getLogger("shelldon.broker")

#: One retry after the first transient failure (AD-2 basic retry; chain is Epic 2).
_MAX_ATTEMPTS = 2


async def handle_job(job: Job, provider: LLMProvider) -> Result:
    """Call the provider for `job`, retrying once on a transient error."""
    last_error: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = await provider.complete(job.payload)
            return Result(ok=True, payload=text)
        except TransientProviderError as exc:
            last_error = f"transient provider error: {exc}"
            log.warning("provider transient error (attempt %d/%d): %s", attempt, _MAX_ATTEMPTS, exc)
            continue
        except PermanentProviderError as exc:
            return Result(ok=False, error=f"provider error: {exc}")
        except Exception as exc:
            # Anything the provider didn't classify must still surface as a Result,
            # never an exception across the bus. Unexpected == non-retryable.
            log.warning("unexpected provider error: %s", type(exc).__name__)
            return Result(ok=False, error=f"unexpected provider error: {type(exc).__name__}")
    return Result(ok=False, error=last_error or "provider unavailable")
