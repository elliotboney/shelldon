"""AC1 real-fork integration — Linux-gated.

macOS aborts fork-without-exec once Apple frameworks initialize, and the prod
target is Linux (Pi), so this skips on darwin. There is no Linux CI yet, so on
macOS dev this is a known coverage gap; it runs on the Pi / a Linux runner. The
COW Private_Dirty RAM-ceiling proof (smaps) is a separate Linux-CI follow-up.
"""

import asyncio
import gc
import sys

import pytest

from shelldon.contracts import Actor, MsgKind
from shelldon.core.bus import BusServer, connect, read_frame
from shelldon.worker.forkserver import ForkServer

pytestmark = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="fork-without-exec is unsafe on macOS frameworks; prod target is Linux",
)


async def test_real_fork_worker_sends_job_then_exits(sock_path):
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        b_reader, b_writer = await connect(sock_path, Actor.BROKER)
        await asyncio.sleep(0.05)

        fs = ForkServer(sock_path)  # real os.fork() spawn + waitpid reap, gc-managed
        await fs.preload()

        pid = await fs.spawn_turn("turn-fork", "ping")
        assert isinstance(pid, int) and pid > 0  # a real child was forked

        got = await asyncio.wait_for(read_frame(b_reader), timeout=2.0)
        assert got.kind is MsgKind.JOB and got.turn_id == "turn-fork"
        assert got.src is Actor.WORKER

        await fs.reap_current()  # child reaped (RAM reclaimed), ≤1 guard released
        assert fs.worker_in_flight is False

        b_writer.close()
        await b_writer.wait_closed()
    finally:
        await srv.stop()
        # Don't leak GC state into other tests on the Linux runner.
        gc.unfreeze()
        gc.enable()
