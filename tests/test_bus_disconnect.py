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


async def test_stop_with_idle_connected_client_does_not_hang(sock_path):
    """Regression: an idle client that stays connected (never disconnects) must not
    block stop(). Server.wait_closed() (3.13) waits on handler tasks; a client parked
    in read_frame would hang shutdown unless the hub cancels its handlers."""
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    # Two idle clients: one settled, one connected right before stop() (still
    # mid-registration, not yet in _conns) — both handler paths must be cancelled.
    settled_reader, settled_writer = await connect(sock_path, Actor.DISPLAY)
    await asyncio.sleep(0.05)
    racing_reader, racing_writer = await connect(sock_path, Actor.PLUGIN_HOST)

    await asyncio.wait_for(srv.stop(), timeout=2.0)  # must return promptly, not hang

    settled_writer.close()
    racing_writer.close()
