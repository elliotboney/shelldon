"""AC1 integration: a Job sent over the bus reaches the registered broker, and its
Result lands in core's inbox with the echoed turn_id. Fake provider, no network.
"""

import asyncio

import pytest

from shelldon.broker.service import run_broker
from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import BusServer, connect, write_frame


class _OK:
    def __init__(self, text="pong"):
        self.text = text

    async def complete(self, prompt):
        return self.text


async def test_job_over_bus_yields_result_to_core(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    broker_task = asyncio.create_task(run_broker(sock_path, [_OK("pong")]))
    try:
        await asyncio.sleep(0.05)  # let the broker register as BROKER

        reader, w = await connect(sock_path, Actor.WORKER)
        job = Envelope(
            id="j1", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER,
            body=Job(payload="ping"), turn_id="turn-7",
        )
        await write_frame(w, job)

        res_env = await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0)
        assert res_env.kind is MsgKind.RESULT
        assert isinstance(res_env.body, Result)
        assert res_env.body.ok and res_env.body.payload == "pong"
        assert res_env.src is Actor.BROKER
        assert res_env.turn_id == "turn-7"  # echoed for core's fencing (AD-12)
        w.close()
        await w.wait_closed()
    finally:
        broker_task.cancel()
        await asyncio.gather(broker_task, return_exceptions=True)
        await srv.stop()
