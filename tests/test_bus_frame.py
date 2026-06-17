"""AC1: 4-byte big-endian length-prefixed msgspec Envelope framing.

Tests the codec directly via a StreamReader fed in chunks — proving a frame
split across reads still reassembles, with no socket needed.
"""

import asyncio

import pytest

from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import MAX_FRAME_BYTES, read_frame, write_frame


class _Collector:
    """A minimal StreamWriter stand-in that captures written bytes."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass


def _job_env():
    return Envelope(
        id="e1", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER, body=Job(payload="hi")
    )


async def _frame_bytes(env):
    c = _Collector()
    await write_frame(c, env)
    return bytes(c.buf)


async def test_frame_roundtrip():
    env = _job_env()
    raw = await _frame_bytes(env)
    assert int.from_bytes(raw[:4], "big") == len(raw) - 4  # 4-byte BE length prefix

    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    assert await read_frame(reader) == env


async def test_frame_reassembles_when_split_across_reads():
    env = Envelope(
        id="e2", kind=MsgKind.RESULT, src=Actor.BROKER, dst=Actor.CORE, body=Result(ok=True)
    )
    raw = await _frame_bytes(env)

    reader = asyncio.StreamReader()
    # Dribble the frame in 3-byte chunks so length and payload span multiple reads.
    for i in range(0, len(raw), 3):
        reader.feed_data(raw[i : i + 3])
    reader.feed_eof()
    assert await read_frame(reader) == env


async def test_clean_eof_returns_none():
    reader = asyncio.StreamReader()
    reader.feed_eof()
    assert await read_frame(reader) is None


async def test_oversized_length_raises_before_allocating():
    """A corrupt/oversized length prefix is a framing error (ValueError), not a
    silent allocation — the hub uses this to close the untrustworthy connection."""
    reader = asyncio.StreamReader()
    reader.feed_data((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
    reader.feed_eof()
    with pytest.raises(ValueError):
        await read_frame(reader)


async def test_mid_payload_truncation_is_eof_not_malformed():
    """A peer dying mid-payload is a disconnect — read_frame returns None (EOF),
    not a malformed-frame error, so the hub logs a clean exit."""
    reader = asyncio.StreamReader()
    reader.feed_data((100).to_bytes(4, "big"))  # claims 100 bytes...
    reader.feed_data(b"only-ten!!")  # ...but only 10 arrive, then EOF
    reader.feed_eof()
    assert await read_frame(reader) is None
