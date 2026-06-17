"""AC1 isolation test: the CLI adapter round-trips against a REAL BusServer with a
STUB core. Drives the injected inbound/outbound seams (no real TTY) to prove:

  (a) an owner line -> an INBOUND_MSG envelope in core's inbox;
  (b) a core OUTBOUND_MSG -> rendered to the adapter's outbound sink;
  (c) owner EOF -> run_cli_transport returns cleanly (both loops torn down).

Story 1.8 then confirms this wiring against the real arbiter/worker.
"""

import asyncio

from shelldon.contracts import (
    SCHEMA_VERSION,
    Actor,
    Envelope,
    InboundMessage,
    MsgKind,
    OutboundMessage,
    encode,
)
from shelldon.core.bus import BusServer, write_frame
from shelldon.transport.cli import run_cli_transport


class _Source:
    """A controllable inbound line source: `feed()` queues a line, `close()` ends
    the stream (the owner's Ctrl-D). Stays open until explicitly closed so the
    outbound half can be exercised before teardown.
    """

    def __init__(self):
        self._q: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, line: str) -> None:
        self._q.put_nowait(line)

    def close(self) -> None:
        self._q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def _transport_writer(srv: BusServer, timeout: float = 1.0):
    """Wait until the CLI adapter has registered as CHAT_TRANSPORT, return its writer."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        writer = srv._registry.get(Actor.CHAT_TRANSPORT)
        if writer is not None:
            return writer
        await asyncio.sleep(0.01)
    raise AssertionError("CLI transport never registered as CHAT_TRANSPORT")


async def test_cli_transport_round_trip(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()

    source = _Source()
    rendered: list[str] = []

    async def sink(text: str) -> None:
        rendered.append(text)

    transport_task = asyncio.create_task(
        run_cli_transport(sock_path, inbound=source, outbound=sink)
    )
    try:
        # (a) owner line -> INBOUND_MSG in core's inbox
        source.feed("hello pet")
        got = await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0)
        assert got.kind is MsgKind.INBOUND_MSG
        assert got.src is Actor.CHAT_TRANSPORT
        assert got.dst is Actor.CORE
        assert isinstance(got.body, InboundMessage)
        assert got.body.text == "hello pet"

        # (b) stub core sends an outbound reply routed to the adapter
        t_writer = await _transport_writer(srv)
        reply = Envelope(
            id="r1",
            kind=MsgKind.OUTBOUND_MSG,
            src=Actor.CORE,
            dst=Actor.CHAT_TRANSPORT,
            body=OutboundMessage(text="hi back"),
        )
        await write_frame(t_writer, reply)

        for _ in range(100):  # let the outbound loop render it
            if rendered:
                break
            await asyncio.sleep(0.01)
        assert rendered == ["hi back"]

        # (c) owner EOF -> the adapter returns cleanly (no hang)
        source.close()
        await asyncio.wait_for(transport_task, timeout=1.0)
        assert transport_task.done() and transport_task.exception() is None
    finally:
        if not transport_task.done():
            transport_task.cancel()
            await asyncio.gather(transport_task, return_exceptions=True)
        await srv.stop()


async def test_outbound_loop_skips_invalid_frame_and_continues(sock_path):
    """One malformed frame must NOT kill the adapter: `_outbound_loop` catches the
    `msgspec.ValidationError` from `read_frame`, `continue`s, and the VALID frame
    sent right after it still renders — proving the bad frame was skipped, not fatal.
    """
    srv = BusServer(socket_path=sock_path)
    await srv.start()

    source = _Source()
    rendered: list[str] = []

    async def sink(text: str) -> None:
        rendered.append(text)

    transport_task = asyncio.create_task(
        run_cli_transport(sock_path, inbound=source, outbound=sink)
    )
    try:
        t_writer = await _transport_writer(srv)

        # A frame that frames fine (4-byte big-endian prefix + msgpack body, same as
        # frame.py `_write_prefixed`) but is an INVALID envelope: an unsupported
        # schema version `v`, which `contracts.decode` rejects with ValidationError
        # inside `_outbound_loop`. `__post_init__` only checks kind<->body, so this
        # constructs cleanly; the version check lives in `decode`.
        bad = Envelope(
            id="bad1",
            kind=MsgKind.OUTBOUND_MSG,
            src=Actor.CORE,
            dst=Actor.CHAT_TRANSPORT,
            body=OutboundMessage(text="should never render"),
            v=SCHEMA_VERSION + 999,
        )
        payload = encode(bad)
        t_writer.write(len(payload).to_bytes(4, "big") + payload)
        await t_writer.drain()

        # A VALID outbound right after the bad one — if the loop survived the skip,
        # this still reaches the sink.
        good = Envelope(
            id="good1",
            kind=MsgKind.OUTBOUND_MSG,
            src=Actor.CORE,
            dst=Actor.CHAT_TRANSPORT,
            body=OutboundMessage(text="still alive"),
        )
        await write_frame(t_writer, good)

        for _ in range(100):  # let the outbound loop skip the bad and render the good
            if rendered:
                break
            await asyncio.sleep(0.01)
        # Only the valid frame rendered: the bad one was dropped, not rendered, not fatal.
        assert rendered == ["still alive"]

        # Owner EOF -> clean return (both loops torn down).
        source.close()
        await asyncio.wait_for(transport_task, timeout=1.0)
        assert transport_task.done() and transport_task.exception() is None
    finally:
        if not transport_task.done():
            transport_task.cancel()
            await asyncio.gather(transport_task, return_exceptions=True)
        await srv.stop()


async def test_outbound_loop_exits_on_hub_disconnect(sock_path):
    """When the hub goes away (`read_frame` -> None), `_outbound_loop` returns, which
    tears down the adapter. Here the inbound `_Source` stays OPEN, so the only thing
    that can end `run_cli_transport` is the hub disconnect — not an owner EOF.
    """
    srv = BusServer(socket_path=sock_path)
    await srv.start()

    source = _Source()  # never closed: the EXIT must be driven by the hub, not inbound

    async def sink(text: str) -> None:  # pragma: no cover - no outbound traffic here
        pass

    transport_task = asyncio.create_task(
        run_cli_transport(sock_path, inbound=source, outbound=sink)
    )
    try:
        await _transport_writer(srv)  # wait until registered before yanking the hub

        # Hub goes away: stop() closes the server-side connection -> the adapter's
        # reader hits EOF -> read_frame returns None -> outbound loop ends -> teardown.
        await srv.stop()

        await asyncio.wait_for(transport_task, timeout=1.0)
        assert transport_task.done() and transport_task.exception() is None
    finally:
        if not transport_task.done():
            transport_task.cancel()
            await asyncio.gather(transport_task, return_exceptions=True)
