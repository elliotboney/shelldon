"""Length-prefixed Envelope framing over asyncio streams (AD-4).

Wire format: 4-byte big-endian length + msgspec(msgpack) Envelope bytes. The
msgpack encode/decode (and the closed-header / version enforcement) live in
`contracts`; this module only adds and strips the length prefix.
"""

import asyncio

from shelldon.contracts import Envelope, decode, encode

#: Reject any frame claiming to be larger than this — a corrupt length prefix
#: must not trigger an unbounded allocation. 8 MiB is far above any real envelope.
MAX_FRAME_BYTES = 8 * 1024 * 1024


async def write_frame(writer, env: Envelope) -> None:
    """Encode `env` and write it as a length-prefixed frame, then drain."""
    payload = encode(env)
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> Envelope | None:
    """Read one length-prefixed frame and decode it.

    Returns None on a clean EOF (the peer closed between frames). Raises
    `ValueError` if the prefix claims an implausible size.
    """
    try:
        header = await reader.readexactly(4)
        length = int.from_bytes(header, "big")
        if length > MAX_FRAME_BYTES:
            raise ValueError(f"frame length {length} exceeds cap {MAX_FRAME_BYTES}")
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        # EOF — at a frame boundary (clean) or mid-payload (peer died mid-send).
        # Either way the peer is gone; report it as a disconnect, not a bad frame.
        return None
    return decode(payload)


async def connect(socket_path: str):
    """Open a bus client connection. Returns (reader, writer)."""
    return await asyncio.open_unix_connection(path=socket_path)
