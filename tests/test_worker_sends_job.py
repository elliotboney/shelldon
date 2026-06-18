"""Story 4.5: the worker sends a Job, awaits the broker's Completion, and emits a
Result (src=WORKER) to core — the reshaped write-back round-trip (was fire-and-forget)."""

import asyncio

import pytest

from shelldon.contracts import Actor, Completion, Envelope, Job, MsgKind, Result
from shelldon.core.bus import BusServer, connect, read_frame, write_frame
from shelldon.worker.worker import run_worker


async def test_worker_round_trips_job_completion_result(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        # A stub broker so the hub can route JOB→BROKER and we can answer it.
        b_reader, b_writer = await connect(sock_path, Actor.BROKER)
        await asyncio.sleep(0.05)

        worker = asyncio.create_task(run_worker(sock_path, "turn-9", "ping"))

        # The broker receives the Job from the worker...
        job = await asyncio.wait_for(read_frame(b_reader), timeout=1.0)
        assert job.kind is MsgKind.JOB
        assert isinstance(job.body, Job) and job.body.payload == "ping"
        assert job.src is Actor.WORKER
        assert job.turn_id == "turn-9"

        # ...and returns a Completion (routed COMPLETION→WORKER, not Result→core).
        await write_frame(
            b_writer,
            Envelope(
                id="c1", kind=MsgKind.COMPLETION, src=Actor.BROKER, dst=Actor.WORKER,
                body=Completion(ok=True, payload="pong"), turn_id="turn-9",
            ),
        )

        # The WORKER (not the broker) emits the Result to core, echoing the turn_id.
        res = await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0)
        assert res.kind is MsgKind.RESULT
        assert isinstance(res.body, Result)
        assert res.body.ok and res.body.payload == "pong"
        assert res.body.proposed_ops == []  # plain reply, no ops block
        assert res.src is Actor.WORKER
        assert res.turn_id == "turn-9"

        await asyncio.wait_for(worker, timeout=1.0)
        b_writer.close()
        await b_writer.wait_closed()
    finally:
        await srv.stop()


async def test_worker_emits_failure_result_when_broker_silent(sock_path, monkeypatch):
    """A broker that never answers → the worker times out and STILL emits a failure
    Result (so core degrades), then exits — it never blocks forever (which would never
    reap and would stick the ≤1 bound, AD-9)."""
    import shelldon.worker.worker as _worker

    monkeypatch.setattr(_worker, "_COMPLETION_TIMEOUT_S", 0.2)
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        b_reader, _b_writer = await connect(sock_path, Actor.BROKER)
        await asyncio.sleep(0.05)

        worker = asyncio.create_task(run_worker(sock_path, "turn-x", "ping"))
        await asyncio.wait_for(read_frame(b_reader), timeout=1.0)  # broker sees the Job, never answers

        res = await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0)
        assert res.kind is MsgKind.RESULT
        assert res.body.ok is False and res.body.error
        assert res.src is Actor.WORKER and res.turn_id == "turn-x"
        await asyncio.wait_for(worker, timeout=1.0)
    finally:
        await srv.stop()
