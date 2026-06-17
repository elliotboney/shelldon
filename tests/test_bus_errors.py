"""Hub robustness under bad input and dead targets (review findings 1 & 2)."""

import asyncio

import pytest

from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import (
    MAX_FRAME_BYTES,
    BusServer,
    connect,
    read_frame,
    write_frame,
)


async def test_oversized_frame_closes_connection_but_hub_survives(sock_path):
    """A framing error (oversized length) leaves the stream untrustworthy — the
    hub closes that connection rather than continuing on a misaligned stream, and
    keeps serving other clients."""
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        reader, writer = await connect(srv.socket_path)
        # Raw oversized header (bypassing write_frame) — declares a huge payload.
        writer.write((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        await writer.drain()

        # The hub drops us: our side reads EOF (None).
        assert await asyncio.wait_for(read_frame(reader), timeout=1.0) is None

        # The hub is still alive for a fresh client: RESULT routes to core_inbox.
        _, w2 = await connect(srv.socket_path)
        res = Envelope(id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE, body=Result(ok=True))
        await write_frame(w2, res)
        assert await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0) == res
    finally:
        await srv.stop()


class _DeadWriter:
    """A registered target whose write fails — simulates a peer that dropped
    between registration and the next routed frame."""

    def write(self, data):
        raise ConnectionResetError("target gone")

    async def drain(self):
        pass

    def close(self):
        pass


async def test_write_to_dead_target_keeps_source_alive(sock_path):
    """A failed write to a dead *target* must not kill the *source* connection."""
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        srv._registry[Actor.BROKER] = _DeadWriter()  # broker "connected" but dead

        _, w = await connect(srv.socket_path)
        # JOB -> BROKER: routing this raises inside _route; it must be swallowed.
        job = Envelope(id="j", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER, body=Job(payload="x"))
        await write_frame(w, job)

        # The worker connection is still alive: a follow-up RESULT still routes.
        res = Envelope(id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE, body=Result(ok=True))
        await write_frame(w, res)
        assert await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0) == res
    finally:
        await srv.stop()
