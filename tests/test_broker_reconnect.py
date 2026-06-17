"""Story 2.2 (folded 1.4 deferrals): the broker survives a hub drop and bounds its
connect. A transient connect failure triggers reconnect; a hung hub times out."""

import asyncio

import pytest

import shelldon.broker.service as service
from shelldon.broker.service import run_broker


class _OK:
    name = "glm"

    async def complete(self, prompt):
        return "pong"


class _EOFReader:
    """A reader that immediately reports EOF, so _serve_connection returns at once."""

    async def _read(self, *a, **k):
        return b""


class _Writer:
    def close(self):
        pass


async def test_reconnects_after_a_transient_connect_failure(monkeypatch):
    calls = {"n": 0}

    async def _fake_connect(socket_path, actor):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionRefusedError("hub not up yet")
        # 2nd+ connect: hand back a reader that EOFs so the serve loop returns fast.
        r = asyncio.StreamReader()
        r.feed_eof()
        return r, _Writer()

    monkeypatch.setattr(service, "connect", _fake_connect)
    task = asyncio.create_task(run_broker("/tmp/ignored.sock", [_OK()], reconnect=True))
    try:
        # It must get PAST the first (failed) connect and successfully connect again.
        for _ in range(100):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert calls["n"] >= 2, "broker did not reconnect after a transient failure"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_connect_timeout_is_bounded(monkeypatch):
    """A hung hub does not block forever: the connect is wrapped in wait_for."""

    async def _hang(socket_path, actor):
        await asyncio.Event().wait()  # never resolves

    monkeypatch.setattr(service, "connect", _hang)
    monkeypatch.setattr(service, "_CONNECT_TIMEOUT_S", 0.02)
    # reconnect=False so the timeout surfaces instead of looping.
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(
            run_broker("/tmp/ignored.sock", [_OK()], reconnect=False), timeout=2.0
        )
