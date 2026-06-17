"""Characterization tests for two already-implemented branches in the broker's
bus-serve loop (`_serve_connection`):

  1. a non-Job envelope is logged and skipped, never handled (service.py:38);
  2. a clean EOF (`read_frame` -> None) ends the connection cleanly (service.py:36).

Both call `_serve_connection` directly with an in-memory `asyncio.StreamReader`
(fed framed bytes + EOF) and a no-op `_Collector` writer — the hub only routes
JOB->BROKER, so a non-Job can't reach the broker via routing.
"""

import asyncio

import pytest

from shelldon.broker.service import _serve_connection
from shelldon.contracts import Actor, Envelope, MsgKind, Result
from shelldon.core.bus import write_frame


class _Collector:
    """Throwaway writer exposing only what write_frame touches (write/drain)."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass


class _RecordingProvider:
    """Provider stub that fails the test if `complete` is ever called."""

    name = "test"

    def __init__(self):
        self.calls = 0

    async def complete(self, prompt):
        self.calls += 1
        return "pong"


def _result_envelope():
    """A non-Job envelope (RESULT body) — should be skipped by the broker loop."""
    return Envelope(
        id="r1", kind=MsgKind.RESULT, src=Actor.BROKER, dst=Actor.CORE,
        body=Result(ok=True, payload="not-a-job"), turn_id="t1",
    )


async def _reader_for(*envelopes):
    enc = _Collector()
    for env in envelopes:
        await write_frame(enc, env)
    r = asyncio.StreamReader()
    r.feed_data(bytes(enc.buf))
    r.feed_eof()
    return r


async def test_non_job_envelope_is_skipped():
    """A RESULT envelope is logged-and-skipped: the provider is never called."""
    reader = await _reader_for(_result_envelope())
    out = _Collector()
    provider = _RecordingProvider()

    await asyncio.wait_for(_serve_connection(reader, out, [provider]), timeout=5)

    assert provider.calls == 0      # non-Job was skipped, not handled
    assert len(out.buf) == 0        # nothing written back for a skipped frame


async def test_clean_eof_ends_connection():
    """An immediate EOF (no frames) ends the loop promptly without error."""
    reader = asyncio.StreamReader()
    reader.feed_eof()
    out = _Collector()
    provider = _RecordingProvider()

    await asyncio.wait_for(_serve_connection(reader, out, [provider]), timeout=5)

    assert provider.calls == 0
    assert len(out.buf) == 0
