"""The local-CLI chat adapter — the first chat transport (AD-13).

A bidirectional bus client (peer to broker/display): owner lines become
**inbound-message** envelopes to core, and **outbound-message** envelopes from
core are rendered back to the owner. It speaks only the transport-agnostic
message contract in `contracts/` — a Telegram or web adapter is the same shape on
the same contract, added without touching `core/`.

stdin/stdout are injected behind two seams — an `inbound` async line source and an
`outbound` async sink — so the adapter's logic is tested deterministically with no
real TTY (mirrors Story 1.5's injectable fork seam). The defaults wrap the real
terminal. The adapter holds NO model/tool credentials (AD-2/NFR9): it imports no
provider SDK and no broker cred path — mechanically enforced by the import-linter.
"""

import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import msgspec

from shelldon.contracts import Actor, Envelope, InboundMessage, MsgKind, OutboundMessage
from shelldon.core.bus import connect, read_frame, write_frame

log = logging.getLogger("shelldon.transport.cli")

InboundSource = AsyncIterator[str]
OutboundSink = Callable[[str], Awaitable[None]]


async def _default_inbound() -> AsyncIterator[str]:
    """Yield owner lines from stdin without blocking the event loop; EOF (Ctrl-D)
    ends the stream. Production glue only — the tested path injects its own source.
    """
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:  # EOF
            return
        line = line.rstrip("\n")
        if line:
            yield line


async def _default_outbound(text: str) -> None:
    print(text, flush=True)


async def _inbound_loop(writer, inbound: InboundSource) -> None:
    """Owner line -> INBOUND_MSG envelope to core, until the source is exhausted."""
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
            log.warning("CLI transport lost the hub on send (%s); stopping", exc)
            return


async def _outbound_loop(reader, outbound: OutboundSink) -> None:
    """OUTBOUND_MSG from core -> rendered via `outbound`. Mirrors the broker's
    per-frame resilience: a bad message is skipped, a framing error or hub EOF ends
    the loop cleanly — a long-lived adapter must never die on one malformed frame.
    """
    while True:
        try:
            env = await read_frame(reader)
        except msgspec.ValidationError as exc:
            log.warning("CLI transport dropping invalid envelope: %s", exc)
            continue
        except ValueError as exc:
            log.warning("CLI transport hit a framing error, ending: %s", exc)
            return
        if env is None:  # hub gone / clean EOF
            return
        if env.kind is not MsgKind.OUTBOUND_MSG or not isinstance(env.body, OutboundMessage):
            log.warning("CLI transport ignoring non-outbound envelope %s (%s)", env.id, env.kind)
            continue
        await outbound(env.body.text)


async def run_cli_transport(
    socket_path: str,
    *,
    inbound: InboundSource | None = None,
    outbound: OutboundSink | None = None,
) -> None:
    """Run the local-CLI chat adapter as a bus client (AD-13).

    Connects as `Actor.CHAT_TRANSPORT` and runs two concurrent loops: owner lines
    from `inbound` -> INBOUND_MSG to core, and OUTBOUND_MSG from core -> `outbound`.
    Whichever loop ends first (owner EOF, or the hub going away) tears down the
    other and returns. `inbound`/`outbound` default to stdin/stdout.
    """
    if inbound is None:
        inbound = _default_inbound()
    if outbound is None:
        outbound = _default_outbound

    reader, writer = await connect(socket_path, Actor.CHAT_TRANSPORT)
    in_task = asyncio.create_task(_inbound_loop(writer, inbound))
    out_task = asyncio.create_task(_outbound_loop(reader, outbound))
    try:
        done, pending = await asyncio.wait(
            {in_task, out_task}, return_when=asyncio.FIRST_COMPLETED
        )
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
