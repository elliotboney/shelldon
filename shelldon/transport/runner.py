"""Generic chat-transport bus plumbing (AD-13) — shared by every transport adapter.

A transport is just an `inbound` async string source + an `outbound` async sink; the
bus side is identical for CLI, Telegram, or web: connect as `CHAT_TRANSPORT`, turn each
owner string into an INBOUND_MSG to core, render each OUTBOUND_MSG from core via the sink,
with per-frame resilience. Extracted from `cli.py` (Story 1.6) so a new adapter (Story 8.2's
Telegram) reuses it verbatim — speaking ONLY the transport-agnostic contract, never `core/`,
holding no model/tool creds (AD-2/NFR9, import-linter-enforced).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import msgspec

from shelldon.contracts import Actor, Envelope, InboundMessage, MsgKind, OutboundMessage
from shelldon.core.bus import connect, read_frame, write_frame

log = logging.getLogger("shelldon.transport")

InboundSource = AsyncIterator[str]
OutboundSink = Callable[[str], Awaitable[None]]


async def _inbound_loop(writer, inbound: InboundSource) -> None:
    """Owner string -> INBOUND_MSG envelope to core, until the source is exhausted."""
    async for line in inbound:
        env = Envelope(
            id=uuid.uuid4().hex,
            kind=MsgKind.INBOUND_MSG,
            src=Actor.CHAT_TRANSPORT,
            dst=Actor.CORE,
            body=InboundMessage(text=line),
        )
        try:
            await write_frame(writer, env)
        except OSError as exc:
            log.warning("transport lost the hub on send (%s); stopping", exc)
            return


async def _outbound_loop(reader, outbound: OutboundSink) -> None:
    """OUTBOUND_MSG from core -> rendered via `outbound`. Per-frame resilience: a bad
    message is skipped, a framing error or hub EOF ends the loop cleanly — a long-lived
    adapter must never die on one malformed frame."""
    while True:
        try:
            env = await read_frame(reader)
        except msgspec.ValidationError as exc:
            log.warning("transport dropping invalid envelope: %s", exc)
            continue
        except ValueError as exc:
            log.warning("transport hit a framing error, ending: %s", exc)
            return
        if env is None:  # hub gone / clean EOF
            return
        if env.kind is not MsgKind.OUTBOUND_MSG or not isinstance(env.body, OutboundMessage):
            log.warning("transport ignoring non-outbound envelope %s (%s)", env.id, env.kind)
            continue
        await outbound(env.body.text)


async def run_transport(socket_path: str, inbound: InboundSource, outbound: OutboundSink) -> None:
    """Run a chat adapter as a bus client (AD-13): connect as `Actor.CHAT_TRANSPORT` and run
    two concurrent loops — `inbound` strings -> INBOUND_MSG to core, OUTBOUND_MSG from core ->
    `outbound`. Whichever loop ends first (owner stream end, or the hub going away) tears down
    the other and returns."""
    reader, writer = await connect(socket_path, Actor.CHAT_TRANSPORT)
    in_task = asyncio.create_task(_inbound_loop(writer, inbound))
    out_task = asyncio.create_task(_outbound_loop(reader, outbound))
    try:
        done, pending = await asyncio.wait({in_task, out_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise a genuine loop failure (a cancellation is not in `done`).
        for task in done:
            task.result()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
