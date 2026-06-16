"""AC3: a client drop doesn't crash the hub; a reconnecting client resumes."""

import asyncio

import pytest

from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import BusServer, connect, read_frame, write_frame


async def test_disconnect_then_reconnect(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        # A client connects then drops abruptly.
        _, doomed = await connect(srv.socket_path, Actor.WORKER)
        doomed.close()
        await doomed.wait_closed()
        await asyncio.sleep(0.05)  # hub observes the disconnect

        # The hub is still up: a broker connects, a worker sends a JOB, it routes.
        b_reader, b_writer = await connect(srv.socket_path, Actor.BROKER)
        await asyncio.sleep(0.05)

        _, w_writer = await connect(srv.socket_path, Actor.WORKER)
        job = Envelope(id="j", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER, body=Job(payload="x"))
        await write_frame(w_writer, job)

        got = await asyncio.wait_for(read_frame(b_reader), timeout=1.0)
        assert got == job
    finally:
        await srv.stop()
