"""Broker bus-client loop (AD-2/AD-4): receive Jobs, answer with Results.

Thin glue over `handle_job` — connects to the hub as BROKER, reads Job envelopes,
and writes the resulting Result back (routed RESULT->CORE), echoing the Job's
`turn_id` so core can fence later (AD-12). Full end-to-end turn wiring is Story 1.8.

The read/write loop mirrors the hub's per-frame resilience (server.py): a bad
message is skipped, a framing error or a vanished hub ends the connection cleanly
— a long-lived broker must never die on one malformed frame or a dropped peer.
"""

import logging
import uuid

import msgspec

from shelldon.broker.broker import handle_job
from shelldon.broker.provider import LLMProvider
from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import connect, read_frame, write_frame

log = logging.getLogger("shelldon.broker")


async def _serve_connection(reader, writer, provider: LLMProvider) -> None:
    """Serve Job→Result over one connected (reader, writer) until it ends."""
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
        result: Result = await handle_job(env.body, provider)
        out = Envelope(
            id=uuid.uuid4().hex,
            kind=MsgKind.RESULT,
            src=Actor.BROKER,
            dst=Actor.CORE,
            body=result,
            turn_id=env.turn_id,
        )
        try:
            await write_frame(writer, out)
        except OSError as exc:
            log.warning("broker lost the hub mid-reply (%s); ending connection", exc)
            break


async def run_broker(socket_path: str, chain: list[LLMProvider]) -> None:
    """Connect as BROKER and serve Job→Result over the bus until the hub closes.

    `chain` is the ordered provider chain (Story 2.1). 2.1 executes the **primary**
    (`chain[0]`) with the existing single-retry; Story 2.2 changes `_serve_connection`
    to advance through the chain on failure (the single fallback seam).
    """
    if not chain:
        raise RuntimeError("run_broker requires a non-empty provider chain")
    reader, writer = await connect(socket_path, Actor.BROKER)
    try:
        await _serve_connection(reader, writer, chain[0])
    finally:
        writer.close()
