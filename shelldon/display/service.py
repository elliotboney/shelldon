"""The long-lived display service (AD-5): a pure-receiver bus client that renders
core's pushed face/state snapshots, latest-wins per region, coalescing under
E-Ink's seconds-scale refresh (NFR3).

It is NOT a simple read->render loop — that would queue a backlog of frames the
slow panel can never catch up to. Instead: an **intake loop** stages the newest
accepted snapshot per region in a single slot (dropping stale `seq`s at the door),
and a **render loop** draws the latest pending then loops. While a slow
`renderer.render()` is in flight, intake overwrites the pending slot, so the next
draw is the newest — intermediate frames coalesce away (no backlog, NFR1/NFR3).

Per-frame resilience mirrors the broker/CLI: a bad message is skipped, a framing
error or hub disconnect ends the service cleanly (a display crash must never take
down the soul — AD-13 / Consistency Conventions). The real core-side push of face
state is Story 1.8 / Epic 3; here the snapshots come from whoever core is.
"""

import asyncio
import logging

import msgspec

from shelldon.contracts import Actor, MsgKind, Region, StateSnapshot
from shelldon.core.bus import connect, read_frame
from shelldon.display.renderer import Renderer

log = logging.getLogger("shelldon.display")


async def _intake_loop(
    reader,
    latest_seq: dict[Region, int],
    pending: dict[Region, StateSnapshot],
    signal: asyncio.Event,
) -> None:
    """Read snapshots; apply latest-wins per region; stage the newest for the
    render loop. Ends cleanly on a framing error or hub disconnect."""
    while True:
        try:
            env = await read_frame(reader)
        except msgspec.ValidationError as exc:
            log.warning("display dropping invalid envelope: %s", exc)
            continue
        except ValueError as exc:
            log.warning("display hit a framing error, ending: %s", exc)
            return
        if env is None:  # hub gone / clean EOF
            return
        if env.kind is not MsgKind.STATE_SNAPSHOT or not isinstance(env.body, StateSnapshot):
            log.warning("display ignoring non-snapshot envelope %s (%s)", env.id, env.kind)
            continue
        snap = env.body
        # Latest-wins (AD-5): drop a snapshot whose seq is not STRICTLY greater than
        # the latest accepted for its region — covers stale AND duplicate seqs.
        if snap.seq <= latest_seq.get(snap.region, -1):
            continue
        latest_seq[snap.region] = snap.seq
        pending[snap.region] = snap  # single slot per region — newest wins
        signal.set()


async def _render_loop(
    renderer: Renderer,
    pending: dict[Region, StateSnapshot],
    signal: asyncio.Event,
) -> None:
    """Draw the latest pending snapshot(s); coalesce bursts that arrived during a
    slow render. Runs until cancelled (the intake loop owns termination)."""
    while True:
        await signal.wait()
        signal.clear()
        # Take + clear the pending set atomically (no await between), then render.
        # Arrivals during the slow render re-populate `pending`, so the next pass
        # draws only the newest per region — the backlog never grows.
        batch = list(pending.values())
        pending.clear()
        for snap in batch:
            await renderer.render(snap)


async def run_display(socket_path: str, renderer: Renderer) -> None:
    """Run the display compositor as a bus client (AD-5).

    Connects as `Actor.DISPLAY` (the first pure-receiver actor — registration alone
    makes it addressable) and runs the intake + render loops. The intake loop owns
    termination: when the hub goes away it ends, and the render loop is torn down.
    """
    reader, writer = await connect(socket_path, Actor.DISPLAY)
    latest_seq: dict[Region, int] = {}
    pending: dict[Region, StateSnapshot] = {}
    signal = asyncio.Event()

    intake = asyncio.create_task(_intake_loop(reader, latest_seq, pending, signal))
    render = asyncio.create_task(_render_loop(renderer, pending, signal))
    try:
        done, still = await asyncio.wait(
            {intake, render}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in still:
            task.cancel()
        await asyncio.gather(*still, return_exceptions=True)
        # Re-raise a genuine loop failure (a cancellation is not in `done`).
        for task in done:
            task.result()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
