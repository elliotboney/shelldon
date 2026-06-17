"""AC2: core routes each envelope by the static kind->destination table.

JOB -> BROKER (forwarded to the broker's connection); RESULT -> CORE (delivered
to core's in-process inbox, never over a socket — core is the hub AND a dest).
"""

import asyncio

import pytest

from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import BusServer, connect, read_frame, write_frame


async def _server(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    return srv


async def test_job_routed_to_broker(sock_path):
    srv = await _server(sock_path)
    try:
        # Broker connects and registers explicitly as BROKER on connect.
        b_reader, b_writer = await connect(srv.socket_path, Actor.BROKER)
        await asyncio.sleep(0.05)  # let the hub process the registration

        # A worker sends a JOB; the table routes JOB -> BROKER.
        w_reader, w_writer = await connect(srv.socket_path, Actor.WORKER)
        job = Envelope(id="j1", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER, body=Job(payload="think"))
        await write_frame(w_writer, job)

        got = await asyncio.wait_for(read_frame(b_reader), timeout=1.0)
        assert got == job
    finally:
        await srv.stop()


async def test_result_routed_to_core_inbox(sock_path):
    srv = await _server(sock_path)
    try:
        _, w_writer = await connect(srv.socket_path, Actor.WORKER)
        res = Envelope(id="r1", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE, body=Result(ok=True, payload="done"))
        await write_frame(w_writer, res)

        got = await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0)
        assert got == res
    finally:
        await srv.stop()
