"""Broker loop resilience (review findings 3 & 4): a malformed/oversized frame or
a vanished hub ends the connection cleanly, never crashing the broker."""

import asyncio

import pytest

from shelldon.broker.service import _serve_connection
from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import MAX_FRAME_BYTES, read_frame, write_frame


class _OK:
    async def complete(self, prompt):
        return "pong"


class _Collector:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass


class _RaisingWriter(_Collector):
    def write(self, data):
        raise BrokenPipeError("hub gone")


def _job(turn_id="t1"):
    return Envelope(
        id="j", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER,
        body=Job(payload="ping"), turn_id=turn_id,
    )


async def _reader_with_job(*, then_oversized=False):
    enc = _Collector()
    await write_frame(enc, _job())
    r = asyncio.StreamReader()
    r.feed_data(bytes(enc.buf))
    if then_oversized:
        r.feed_data((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
    r.feed_eof()
    return r


async def test_serve_processes_job_then_eof():
    reader = await _reader_with_job()
    out = _Collector()
    await _serve_connection(reader, out, _OK())

    # One RESULT frame written back, turn_id echoed.
    rr = asyncio.StreamReader()
    rr.feed_data(bytes(out.buf))
    rr.feed_eof()
    res = await read_frame(rr)
    assert res.kind is MsgKind.RESULT and isinstance(res.body, Result)
    assert res.body.ok and res.body.payload == "pong" and res.turn_id == "t1"


async def test_serve_survives_write_failure():
    """Hub vanishes mid-reply: the loop ends cleanly instead of crashing (finding 4)."""
    reader = await _reader_with_job()
    await _serve_connection(reader, _RaisingWriter(), _OK())  # must not raise


async def test_serve_survives_framing_error():
    """An oversized frame ends the connection without killing the broker (finding 3)."""
    reader = await _reader_with_job(then_oversized=True)
    out = _Collector()
    await _serve_connection(reader, out, _OK())  # must not raise
    assert len(out.buf) > 0  # the valid job before the bad frame was still answered
