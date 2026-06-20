"""The local-CLI chat adapter — the first chat transport (AD-13).

A bidirectional bus client (peer to broker/display): owner stdin lines become
**inbound-message** envelopes to core, and **outbound-message** envelopes from core are
printed back. It speaks only the transport-agnostic contract; the bus plumbing lives in
`transport/runner.py` (shared with Telegram, Story 8.2). stdin/stdout are injected behind
an `inbound` async line source + an `outbound` async sink, so the adapter is tested with no
real TTY (mirrors Story 1.5's fork seam); the defaults wrap the real terminal. The adapter
holds NO model/tool credentials (AD-2/NFR9) — import-linter-enforced.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator

from shelldon.transport.runner import InboundSource, OutboundSink, run_transport

log = logging.getLogger("shelldon.transport.cli")


async def _default_inbound() -> AsyncIterator[str]:
    """Yield owner lines from stdin without blocking the event loop; EOF (Ctrl-D) ends the
    stream. Production glue only — the tested path injects its own source."""
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


async def run_cli_transport(
    socket_path: str,
    *,
    inbound: InboundSource | None = None,
    outbound: OutboundSink | None = None,
) -> None:
    """Run the local-CLI chat adapter as a bus client (AD-13). `inbound`/`outbound` default
    to stdin/stdout; the bus loops are the shared `run_transport`."""
    if inbound is None:
        inbound = _default_inbound()
    if outbound is None:
        outbound = _default_outbound
    await run_transport(socket_path, inbound, outbound)
