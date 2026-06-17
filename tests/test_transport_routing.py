"""AC1/AC3: the two new transport message kinds route by the static table on an
UNMODIFIED hub — INBOUND_MSG -> CORE (in-process inbox); OUTBOUND_MSG ->
CHAT_TRANSPORT (the registered adapter connection). Proves the seam is data-driven
(contracts/ rows), not a hub change.
"""

import asyncio

from shelldon.contracts import (
    Actor,
    Envelope,
    InboundMessage,
    MsgKind,
    OutboundMessage,
)
from shelldon.core.bus import BusServer, connect, read_frame, write_frame


async def test_inbound_message_routed_to_core(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        # The chat-transport adapter connects and emits an inbound message.
        _, t_writer = await connect(sock_path, Actor.CHAT_TRANSPORT)
        env = Envelope(
            id="in-1",
            kind=MsgKind.INBOUND_MSG,
            src=Actor.CHAT_TRANSPORT,
            dst=Actor.CORE,
            body=InboundMessage(text="hello pet"),
        )
        await write_frame(t_writer, env)

        got = await asyncio.wait_for(srv.core_inbox.get(), timeout=1.0)
        assert got == env
        assert isinstance(got.body, InboundMessage)
        assert got.body.text == "hello pet"
    finally:
        await srv.stop()


async def test_outbound_message_routed_to_transport(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        # The adapter registers as CHAT_TRANSPORT so core's reply can find it.
        # Keep BOTH stream ends of each connection alive — dropping a StreamWriter
        # lets the transport close, deregistering the actor.
        t_reader, t_writer = await connect(sock_path, Actor.CHAT_TRANSPORT)
        await asyncio.sleep(0.05)  # let the hub process the registration

        # A stand-in core sends an outbound reply; the table routes it to CHAT_TRANSPORT.
        c_reader, c_writer = await connect(sock_path, Actor.CORE)
        env = Envelope(
            id="out-1",
            kind=MsgKind.OUTBOUND_MSG,
            src=Actor.CORE,
            dst=Actor.CHAT_TRANSPORT,
            body=OutboundMessage(text="hi back"),
        )
        await write_frame(c_writer, env)

        got = await asyncio.wait_for(read_frame(t_reader), timeout=1.0)
        assert got == env
        assert isinstance(got.body, OutboundMessage)
        assert got.body.text == "hi back"
    finally:
        await srv.stop()
