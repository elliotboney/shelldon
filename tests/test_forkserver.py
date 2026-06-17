"""AC1/AC2 orchestration (the M0 concurrency test) — fake spawner, no real fork.

Exercises readiness, the ≤1-in-flight bound, serialization, and release-on-failure
deterministically and cross-platform. The real os.fork() path is test_forkserver_fork.py.
"""

import asyncio

import pytest

from shelldon.worker.forkserver import ForkServer, WorkerBusyError


class _FakeSpawner:
    """Records concurrency; a fake 'worker' is in flight from spawn until reap."""

    def __init__(self, fail=False):
        self.fail = fail
        self.spawns = 0
        self.live = 0
        self.max_live = 0

    async def spawn(self, socket_path, turn_id, prompt):
        if self.fail:
            raise RuntimeError("fork failed")
        self.spawns += 1
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        return {"turn_id": turn_id}

    async def reap(self, handle):
        self.live -= 1


def _server(spawner, **kw):
    # manage_gc=False: unit tests never fork, so don't mutate global GC state.
    kw.setdefault("manage_gc", False)
    return ForkServer("/tmp/x.sock", spawn=spawner.spawn, reap=spawner.reap, **kw)


async def test_ready_after_preload():
    fs = _server(_FakeSpawner(), preload_modules=())
    await fs.preload()
    await asyncio.wait_for(fs.ready(), timeout=1.0)  # resolves, doesn't hang


async def test_only_one_worker_in_flight():
    sp = _FakeSpawner()
    fs = _server(sp)
    await fs.preload()

    await fs.spawn_turn("t1", "hi")          # one in flight
    with pytest.raises(WorkerBusyError):
        await fs.spawn_turn("t2", "hi")      # refused, no second fork
    assert sp.spawns == 1
    assert sp.max_live == 1                  # never two workers at once


async def test_serializes_after_reap():
    sp = _FakeSpawner()
    fs = _server(sp)
    await fs.preload()

    await fs.spawn_turn("t1", "hi")
    await fs.reap_current()                  # first worker done
    await fs.spawn_turn("t2", "hi")          # now the next turn proceeds
    assert sp.spawns == 2
    assert sp.max_live == 1


async def test_failed_spawn_releases_guard():
    sp = _FakeSpawner(fail=True)
    fs = _server(sp)
    await fs.preload()

    with pytest.raises(RuntimeError):
        await fs.spawn_turn("t1", "hi")      # spawn raises...
    # ...and the guard is released, so a later turn isn't deadlocked:
    sp.fail = False
    await fs.spawn_turn("t2", "hi")
    assert sp.spawns == 1


async def test_spawn_before_preload_is_refused():
    """AC1: nothing forks before the readiness barrier (preload) completes."""
    sp = _FakeSpawner()
    fs = _server(sp)  # no preload() called
    with pytest.raises(RuntimeError):
        await fs.spawn_turn("t1", "hi")
    assert sp.spawns == 0


class _ChildAlreadyReaped(_FakeSpawner):
    async def reap(self, handle):
        raise ChildProcessError("already reaped by SIGCHLD")


async def test_reap_childprocesserror_releases_guard_no_deadlock():
    """A benign reap race (child already reaped) must not deadlock future turns."""
    sp = _ChildAlreadyReaped()
    fs = _server(sp)
    await fs.preload()
    await fs.spawn_turn("t1", "hi")
    await fs.reap_current()                  # reaper raises ChildProcessError internally
    assert fs.worker_in_flight is False      # guard released, not stuck
    await fs.spawn_turn("t2", "hi")          # next turn proceeds
