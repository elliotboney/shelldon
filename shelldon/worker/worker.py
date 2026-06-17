"""The ephemeral worker child (AD-3): assemble a prompt, proxy to the broker, die.

Fire-and-forget — the worker sends one Job and exits. It does NOT wait for the
Result: the broker's Result routes to CORE (RESULT→CORE), which fences it by
turn_id (AD-12). Real prompt assembly (history + memory) is Story 1.8; here the
prompt is whatever core handed in.
"""

import logging

from shelldon.contracts import Actor, Envelope, Job, MsgKind
from shelldon.core.bus import connect, write_frame

log = logging.getLogger("shelldon.worker")


async def run_worker(socket_path: str, turn_id: str, prompt: str) -> None:
    """Connect as WORKER, send one Job for `turn_id`, then return (the child exits)."""
    reader, writer = await connect(socket_path, Actor.WORKER)
    try:
        await write_frame(
            writer,
            Envelope(
                id=turn_id,
                kind=MsgKind.JOB,
                src=Actor.WORKER,
                dst=Actor.BROKER,
                body=Job(payload=prompt),
                turn_id=turn_id,
            ),
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
