"""AC1/AC2 isolation tests: `run_display` against a REAL BusServer with a STAND-IN
core pushing snapshots and an injected renderer (a recording stub, or a gateable
fake that simulates the slow E-Ink draw). Proves:

  (a) a snapshot renders;
  (b) latest-wins — a stale/duplicate (<= seq) snapshot is dropped;
  (c) a burst arriving during a slow draw coalesces to the latest (no backlog);
  (d) hub disconnect -> run_display returns cleanly.

Story 1.8 then confirms this against real core-pushed face state.
"""

import asyncio

from shelldon.contracts import Actor, Envelope, MsgKind, Region, StateSnapshot
from shelldon.core.bus import BusServer, write_frame
from shelldon.display.renderer import StubRenderer


class _GatedRenderer:
    """Records each snapshot the instant a draw STARTS, then blocks until the test
    `release()`s it — simulating E-Ink's slow, one-at-a-time refresh so coalescing
    is observable and deterministic."""

    def __init__(self) -> None:
        self.rendered: list[StateSnapshot] = []
        self._releases: asyncio.Queue[None] = asyncio.Queue()

    async def render(self, snapshot: StateSnapshot) -> None:
        self.rendered.append(snapshot)
        await self._releases.get()  # hold the draw open until the test lets it finish

    def release(self) -> None:
        self._releases.put_nowait(None)


async def _wait_registered(srv: BusServer, actor: Actor, timeout: float = 1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        w = srv._registry.get(actor)
        if w is not None:
            return w
        await asyncio.sleep(0.01)
    raise AssertionError(f"{actor} never registered")


async def _wait_until(predicate, timeout: float = 1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _snapshot_env(seq: int, face: str, region: Region = Region.FACE) -> Envelope:
    return Envelope(
        id=f"snap-{seq}",
        kind=MsgKind.STATE_SNAPSHOT,
        src=Actor.CORE,
        dst=Actor.DISPLAY,
        body=StateSnapshot(region=region, seq=seq, face=face),
    )


async def _run_display_against(sock_path, renderer):
    """Start a BusServer + run_display, and return the display's registered
    server-side writer so a stand-in core can push snapshots straight to it
    (the hub routing is covered separately in test_display_routing.py). Avoiding a
    second client connection keeps srv.stop() teardown clean. Returns
    (srv, display_task, push_writer); caller cleans up."""
    from shelldon.display.service import run_display

    srv = BusServer(socket_path=sock_path)
    await srv.start()
    display_task = asyncio.create_task(run_display(sock_path, renderer))
    push_writer = await _wait_registered(srv, Actor.DISPLAY)
    return srv, display_task, push_writer


async def _teardown(srv, display_task):
    if not display_task.done():
        display_task.cancel()
        await asyncio.gather(display_task, return_exceptions=True)
    await srv.stop()


async def test_renders_snapshot(sock_path):
    renderer = StubRenderer()
    srv, display_task, push_writer = await _run_display_against(sock_path, renderer)
    try:
        await write_frame(push_writer, _snapshot_env(1, "neutral"))
        await _wait_until(lambda: len(renderer.rendered) == 1)
        drawn = renderer.rendered[0]
        assert drawn.region is Region.FACE
        assert drawn.seq == 1
        assert drawn.face == "neutral"
    finally:
        await _teardown(srv, display_task)


async def test_drops_stale_and_duplicate_seq(sock_path):
    """AC1 latest-wins: a lower seq AND an equal seq are both dropped (<=)."""
    renderer = StubRenderer()
    srv, display_task, push_writer = await _run_display_against(sock_path, renderer)
    try:
        await write_frame(push_writer, _snapshot_env(5, "excited"))
        await _wait_until(lambda: len(renderer.rendered) == 1)

        # A stale (3) and a duplicate (5) must NOT render — neither is > latest seq 5.
        await write_frame(push_writer, _snapshot_env(3, "old"))
        await write_frame(push_writer, _snapshot_env(5, "dup"))
        await asyncio.sleep(0.1)  # give intake time to read+drop both

        assert [s.seq for s in renderer.rendered] == [5]
        assert renderer.rendered[0].face == "excited"
    finally:
        await _teardown(srv, display_task)


async def test_coalesces_burst_to_latest(sock_path):
    """AC2: snapshots arriving during a slow draw coalesce — only the newest draws
    next, intermediate frames never render (no backlog, NFR3)."""
    renderer = _GatedRenderer()
    srv, display_task, push_writer = await _run_display_against(sock_path, renderer)
    try:
        # seq=1 begins drawing and blocks (gate closed).
        await write_frame(push_writer, _snapshot_env(1, "f1"))
        await _wait_until(lambda: len(renderer.rendered) == 1)
        assert [s.seq for s in renderer.rendered] == [1]

        # A burst arrives WHILE seq=1 is still drawing.
        for seq in (2, 3, 4, 5):
            await write_frame(push_writer, _snapshot_env(seq, f"f{seq}"))
        await asyncio.sleep(0.1)  # let intake drain all four into the single slot

        # Let seq=1 finish -> the render loop picks up only the latest pending (5).
        renderer.release()
        await _wait_until(lambda: len(renderer.rendered) == 2)
        assert [s.seq for s in renderer.rendered] == [1, 5]  # 2,3,4 coalesced away

        renderer.release()  # let seq=5 finish so teardown is clean
    finally:
        await _teardown(srv, display_task)


async def test_clean_teardown_on_hub_disconnect(sock_path):
    renderer = StubRenderer()
    srv, display_task, push_writer = await _run_display_against(sock_path, renderer)
    try:
        await srv.stop()  # hub goes away -> intake hits EOF -> run_display returns
        await asyncio.wait_for(display_task, timeout=1.0)
        assert display_task.done() and display_task.exception() is None
    finally:
        if not display_task.done():
            display_task.cancel()
            await asyncio.gather(display_task, return_exceptions=True)
