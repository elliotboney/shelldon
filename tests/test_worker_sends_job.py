"""AC1: the worker connects, sends one Job to the broker, and returns (fire-and-forget)."""

import asyncio

import pytest

from shelldon.contracts import Actor, Job, MsgKind
from shelldon.core.bus import BusServer, connect, read_frame
from shelldon.worker.worker import run_worker


async def test_worker_sends_job_to_broker(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        # A stub broker so the hub has somewhere to route JOB→BROKER.
        b_reader, b_writer = await connect(sock_path, Actor.BROKER)
        await asyncio.sleep(0.05)

        await run_worker(sock_path, "turn-9", "ping")  # connects, sends, returns

        got = await asyncio.wait_for(read_frame(b_reader), timeout=1.0)
        assert got.kind is MsgKind.JOB
        assert isinstance(got.body, Job) and got.body.payload == "ping"
        assert got.src is Actor.WORKER
        assert got.turn_id == "turn-9"

        b_writer.close()
        await b_writer.wait_closed()
    finally:
        await srv.stop()
