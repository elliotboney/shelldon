"""Fork-server (AD-3): a warm parent that forks one ephemeral worker per turn.

The raw os.fork() is injected behind a `spawn` seam so the orchestration (≤1
guard, readiness, reaping) is tested cross-platform without a real fork — macOS
can't safely fork-without-exec, and the prod target is Linux (Pi). In production
this runs as its own single-threaded process (never fork from the asyncio core
loop); 1.5 delivers the mechanism + seam, the process/IPC wiring is Story 1.8.
"""

import asyncio
import gc
import importlib
import logging
import os

log = logging.getLogger("shelldon.forkserver")


class WorkerBusyError(Exception):
    """Raised when a turn is requested while one is already in flight (≤1, AD-9)."""


async def _os_fork_spawn(socket_path: str, turn_id: str, prompt: str):
    """Real fork seam: fork one worker that runs the turn then _exit()s. Linux-only
    in practice (macOS aborts fork-without-exec)."""
    from shelldon.worker.worker import run_worker

    pid = os.fork()
    if pid == 0:  # child
        try:
            asyncio.run(run_worker(socket_path, turn_id, prompt))
        finally:
            os._exit(0)  # reclaim RAM; never run parent teardown in the child
    return pid


async def _os_waitpid_reap(handle) -> None:
    """Reap the worker without threads (thread-free keeps future forks safe)."""
    while True:
        try:
            pid, _ = os.waitpid(handle, os.WNOHANG)
        except ChildProcessError:
            return  # already reaped (e.g. by a SIGCHLD handler) — benign
        if pid != 0:
            return
        await asyncio.sleep(0.01)


class ForkServer:
    """Warm-fork lifecycle: preload+freeze once, then one worker per turn."""

    def __init__(self, socket_path, *, spawn=None, reap=None,
                 preload_modules=(), manage_gc=True):
        self.socket_path = socket_path
        self._spawn = spawn or _os_fork_spawn
        self._reap = reap or _os_waitpid_reap
        self._preload_modules = tuple(preload_modules)
        self._manage_gc = manage_gc
        self._ready = asyncio.Event()
        self._inflight = None
        #: Mechanical ≤1 bound — the spawner physically can't run two workers.
        #: core's Arbiter (the *policy* on when to request a turn) wires in at 1.8.
        self.worker_in_flight = False

    async def preload(self) -> None:
        """Warm the libs and freeze them COW-shared, then raise the readiness barrier.

        Ordering (binding, AD-3): gc.disable() → import → gc.collect() → gc.freeze().
        gc.freeze() exempts the pre-imported objects from GC scans so they stay
        shared after fork. (Refcount writes still dirty pages — the RAM win needs
        the worker not to deep-traverse shared objects.)
        """
        if self._manage_gc:
            gc.disable()
        for mod in self._preload_modules:
            importlib.import_module(mod)
        if self._manage_gc:
            gc.collect()
            gc.freeze()
        self._ready.set()

    async def ready(self) -> None:
        """Block until preload+freeze is done — nothing forks before this."""
        await self._ready.wait()

    async def spawn_turn(self, turn_id: str, prompt: str):
        """Fork exactly one worker for `turn_id`. Raises RuntimeError if preload()
        hasn't completed (AC1 readiness barrier), or WorkerBusyError if one is
        already in flight (≤1, AD-9)."""
        if not self._ready.is_set():
            raise RuntimeError("ForkServer.preload() must complete before spawn_turn()")
        if self.worker_in_flight:
            raise WorkerBusyError(f"a worker is already in flight; refusing turn {turn_id}")
        self.worker_in_flight = True
        try:
            handle = await self._spawn(self.socket_path, turn_id, prompt)
        except BaseException:
            self.worker_in_flight = False  # release on failure — never deadlock future turns
            raise
        self._inflight = handle
        return handle

    async def reap_current(self) -> None:
        """Reap the in-flight worker (RAM reclaimed) and release the ≤1 guard.

        The guard is released even if reaping errors — a benign reap race must
        never deadlock all future turns.
        """
        handle, self._inflight = self._inflight, None
        try:
            if handle is not None:
                await self._reap(handle)
        except ChildProcessError:
            pass  # child already reaped — benign
        finally:
            self.worker_in_flight = False
