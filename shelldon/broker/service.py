"""Broker bus-client loop (AD-2/AD-4): receive Jobs, answer with Completions.

Thin glue over `handle_job` — connects to the hub as BROKER, reads Job envelopes,
and writes the raw provider Completion back to the WORKER (routed COMPLETION->WORKER),
echoing the Job's `turn_id`. The worker (not the broker) parses the reply into a
Result with proposed_ops (Story 4.5) — the broker stays a pure egress boundary,
doing no pet-domain parsing (AD-2).

The read/write loop mirrors the hub's per-frame resilience (server.py): a bad
message is skipped, a framing error or a vanished hub ends the connection cleanly
— a long-lived broker must never die on one malformed frame or a dropped peer.
"""

import asyncio
import logging
import uuid

import msgspec

from shelldon.broker.broker import handle_job_chain
from shelldon.broker.provider import LLMProvider
from shelldon.contracts import Actor, Completion, Envelope, Job, MsgKind, Result
from shelldon.core.bus import connect, read_frame, write_frame

log = logging.getLogger("shelldon.broker")

#: Cap on the initial connect so a hung hub can't block the broker forever (1.4
#: deferral). On timeout the reconnect loop backs off and retries.
_CONNECT_TIMEOUT_S = 5.0

#: Backoff between reconnect attempts (seconds) — a transient hub drop/restart must
#: not kill the broker permanently (1.4 deferral). Module-level so tests set it to 0.
_RECONNECT_BACKOFF_S = 1.0


async def _serve_connection(reader, writer, chain: list[LLMProvider]) -> None:
    """Serve Job→Result over one connected (reader, writer) until it ends.

    Each Job runs through the ordered provider `chain`: a failed call falls through
    to the next provider (Story 2.2); an exhausted chain yields a failure Result.
    """
    while True:
        try:
            env = await read_frame(reader)
        except msgspec.ValidationError as exc:
            log.warning("broker dropping invalid envelope: %s", exc)
            continue
        except ValueError as exc:
            log.warning("broker hit a framing error, ending connection: %s", exc)
            break
        if env is None:  # hub gone / clean EOF
            break
        if env.kind is not MsgKind.JOB or not isinstance(env.body, Job):
            log.warning("broker ignoring non-Job envelope %s (%s)", env.id, env.kind)
            continue
        result: Result = await handle_job_chain(env.body, chain)
        # Return the raw completion to the WORKER, not a Result to core (Story 4.5):
        # the worker parses its own reply into proposed_ops and emits the Result. The
        # broker stays a pure egress boundary — text/error only, no pet-domain parsing
        # (AD-2). turn_id is echoed so the worker (and core's fence) can correlate.
        out = Envelope(
            id=uuid.uuid4().hex,
            kind=MsgKind.COMPLETION,
            src=Actor.BROKER,
            dst=Actor.WORKER,
            body=Completion(ok=result.ok, payload=result.payload, error=result.error),
            turn_id=env.turn_id,
        )
        try:
            await write_frame(writer, out)
        except OSError as exc:
            log.warning("broker lost the hub mid-reply (%s); ending connection", exc)
            break


async def run_broker(socket_path: str, chain: list[LLMProvider], *, reconnect: bool = True) -> None:
    """Connect as BROKER and serve Job→Result over the bus, surviving hub drops.

    `chain` is the ordered provider chain (Story 2.1); each Job falls through it on
    failure (Story 2.2). The connect is timeout-bounded and, with `reconnect=True`
    (default), a dropped/refused hub triggers a backoff-and-retry rather than killing
    the broker (1.4 resilience deferrals folded into 2.2). Cancellation always wins —
    a deliberate shutdown cancels this task and the loop exits cleanly.
    """
    if not chain:
        raise RuntimeError("run_broker requires a non-empty provider chain")
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                connect(socket_path, Actor.BROKER), timeout=_CONNECT_TIMEOUT_S
            )
        except (OSError, asyncio.TimeoutError) as exc:
            if not reconnect:
                raise
            log.warning("broker could not reach the hub (%s); retrying", type(exc).__name__)
            await asyncio.sleep(_RECONNECT_BACKOFF_S)
            continue
        try:
            await _serve_connection(reader, writer, chain)
        finally:
            writer.close()
            try:
                # Block until the transport is fully released, so a fast reconnect
                # can't re-open the same socket while the old writer is still draining.
                await writer.wait_closed()
            except OSError:
                pass  # peer already gone — nothing left to drain
        if not reconnect:
            break
        log.warning("broker connection to the hub ended; reconnecting")
        await asyncio.sleep(_RECONNECT_BACKOFF_S)
