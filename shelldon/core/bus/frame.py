"""Length-prefixed framing over asyncio streams (AD-4).

Wire format: 4-byte big-endian length + msgspec(msgpack) bytes. Two frame kinds
share the prefix: a one-shot **registration** frame (the client's `Actor`, sent
first on connect so the hub can address a receiver-first actor like the broker)
and the **Envelope** stream that follows. The Envelope msgpack encode/decode (and
the closed-header / version enforcement) live in `contracts`.
"""

import asyncio

import msgspec

from shelldon.contracts import Actor, Envelope, decode, encode

#: Reject any frame claiming to be larger than this — a corrupt length prefix
#: must not trigger an unbounded allocation. 8 MiB is far above any real envelope.
MAX_FRAME_BYTES = 8 * 1024 * 1024

_actor_decoder = msgspec.msgpack.Decoder(Actor)


async def _write_prefixed(writer, payload: bytes) -> None:
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def _read_prefixed(reader: asyncio.StreamReader) -> bytes | None:
    """Read one length-prefixed payload. None on EOF (clean or mid-frame)."""
    try:
        header = await reader.readexactly(4)
        length = int.from_bytes(header, "big")
        if length > MAX_FRAME_BYTES:
            raise ValueError(f"frame length {length} exceeds cap {MAX_FRAME_BYTES}")
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        return None


async def write_registration(writer, actor: Actor) -> None:
    """Send the connecting client's identity — the mandatory first frame."""
    await _write_prefixed(writer, msgspec.msgpack.encode(actor))


async def read_registration(reader: asyncio.StreamReader) -> Actor | None:
    """Read the registration frame. None on EOF; raises on an unknown actor."""
    raw = await _read_prefixed(reader)
    if raw is None:
        return None
    return _actor_decoder.decode(raw)


async def write_frame(writer, env: Envelope) -> None:
    """Encode `env` and write it as a length-prefixed frame, then drain."""
    await _write_prefixed(writer, encode(env))


async def read_frame(reader: asyncio.StreamReader) -> Envelope | None:
    """Read one length-prefixed Envelope frame and decode it.

    Returns None on a clean EOF or a peer dying mid-frame (both mean the peer is
    gone). Raises `ValueError` if the prefix claims an implausible size.
    """
    raw = await _read_prefixed(reader)
    if raw is None:
        return None
    return decode(raw)


async def connect(socket_path: str, actor: Actor):
    """Open a bus client connection and register as `actor`. Returns (reader, writer)."""
    reader, writer = await asyncio.open_unix_connection(path=socket_path)
    await write_registration(writer, actor)
    return reader, writer
