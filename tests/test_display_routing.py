"""AC1: a STATE_SNAPSHOT routes by the static table to DISPLAY on an UNMODIFIED
hub, and a pure-receiver actor (the display sends nothing) is addressable purely
from its registration frame. Proves the seam is data-driven (contracts/ row), not
a hub change.
"""

import asyncio

from shelldon.contracts import Actor, Envelope, MsgKind, Region, StateSnapshot
from shelldon.core.bus import BusServer, connect, read_frame, write_frame


async def test_state_snapshot_routed_to_display(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        # The display registers as DISPLAY and then only RECEIVES (pure receiver).
        # Keep BOTH stream ends alive — dropping the writer would close the conn.
        d_reader, d_writer = await connect(sock_path, Actor.DISPLAY)
        await asyncio.sleep(0.05)  # let the hub process the registration

        # A stand-in core pushes a face snapshot; the table routes it to DISPLAY.
        c_reader, c_writer = await connect(sock_path, Actor.CORE)
        env = Envelope(
            id="snap-1",
            kind=MsgKind.STATE_SNAPSHOT,
            src=Actor.CORE,
            dst=Actor.DISPLAY,
            body=StateSnapshot(region=Region.FACE, seq=1, face="neutral"),
        )
        await write_frame(c_writer, env)

        got = await asyncio.wait_for(read_frame(d_reader), timeout=1.0)
        assert got == env
        assert isinstance(got.body, StateSnapshot)
        assert got.body.region is Region.FACE
        assert got.body.seq == 1
        assert got.body.face == "neutral"
    finally:
        await srv.stop()
