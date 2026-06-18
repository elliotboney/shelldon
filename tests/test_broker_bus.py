"""AC1 integration: a Job sent over the bus reaches the registered broker, which
returns a Completion routed back to the WORKER (Story 4.5: the broker no longer emits
a Result to core — the worker does). Fake provider, no network.
"""

import asyncio

import pytest

from shelldon.broker.service import run_broker
from shelldon.contracts import Actor, Completion, Envelope, Job, MsgKind
from shelldon.core.bus import BusServer, connect, read_frame, write_frame


class _OK:
    name = "test"

    def __init__(self, text="pong"):
        self.text = text

    async def complete(self, prompt):
        return self.text


async def test_job_over_bus_yields_completion_to_worker(sock_path):
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

        # The broker answers the WORKER with a Completion (NOT a Result to core).
        comp = await asyncio.wait_for(read_frame(reader), timeout=1.0)
        assert comp.kind is MsgKind.COMPLETION
        assert isinstance(comp.body, Completion)
        assert comp.body.ok and comp.body.payload == "pong"
        assert comp.src is Actor.BROKER
        assert comp.turn_id == "turn-7"  # echoed so the worker stamps the Result (AD-12)
        w.close()
        await w.wait_closed()
    finally:
        broker_task.cancel()
        await asyncio.gather(broker_task, return_exceptions=True)
        await srv.stop()
