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
import signal

log = logging.getLogger("shelldon.forkserver")

#: Bounded-reap deadline (Story 5.0): if a forked child hasn't exited within this long,
#: the reaper escalates to SIGKILL and reclaims it — so an unkillable/wedged child can
#: never spin the reaper forever or hold the ≤1 slot past the turn. Part of the
#: coherent-timeout chain W < R < T (worker self-report 25s < reap SIGKILL 28s < core
#: degrade 30s); R stays below core's turn timeout so the slot frees before/at core's
#: degrade. Module-level so tests inject a small value.
_REAP_TIMEOUT_S = 28.0
_REAP_POLL_S = 0.01

#: After SIGKILL, reclaim the zombie by polling (NOT a blocking waitpid) for this long, so
#: a child stuck in uninterruptible (D-state) kernel sleep can't freeze the asyncio loop.
#: If it still hasn't gone, give up and let the OS reaper collect the zombie (Story 5.0).
_REAP_KILL_GRACE_S = 2.0

#: Upper bound for closing inherited FDs in the fork child. `SC_OPEN_MAX` can be ~1e6 (or
#: the hard rlimit) — closing that many one-by-one would be a per-turn stall on a kernel
#: without close_range(2). The child only inherits a handful of low FDs, so a modest cap
#: covers them all with bounded work (Story 5.0).
_MAX_INHERITED_FD = 4096


class WorkerBusyError(Exception):
    """Raised when a turn is requested while one is already in flight (≤1, AD-9)."""


def _close_inherited_sqlite() -> None:
    """Discard every SQLite connection inherited from the parent across fork().

    SQLite connections are NOT fork-safe. The fork child inherits core's open WAL
    `HistoryStore` connection (its fds + the `-shm` shared-memory mmap). Left in place, the
    `closerange` below closes the inherited `-shm` fd while the mmap lingers — a torn
    shared-memory state that makes the worker's OWN read-only history open fail with
    `SQLITE_PROTOCOL` ("locking protocol") on the Pi (real fork; masked in-process on macOS).
    The worker opens a fresh read handle, so closing the parent's is pure cleanup. Closing
    here (BEFORE closerange) lets SQLite tear the connection down cleanly with its fds intact.
    The child `os._exit`s, so closing every inherited connection has no downside.
    """
    import sqlite3

    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.close()
            except Exception:  # a half-initialised / already-closed handle — ignore
                pass


def _real_drop(uid, gid, *, setgid=os.setgid, setuid=os.setuid, getuid=os.getuid) -> None:
    """Drop to the worker uid/gid in the fork child (AD-6 vault isolation).

    gid BEFORE uid is load-bearing — you cannot setgid once uid is dropped, so
    the order is fixed. Then fail-closed: verify getuid() landed on `uid`,
    raising if it didn't (a silently-undropped child must never run a turn).
    The os calls are injected so the order + verify are testable without setuid.
    """
    setgid(gid)
    setuid(uid)
    landed = getuid()
    if landed != uid:
        raise RuntimeError(f"privilege drop did not take: getuid()={landed} != {uid}")


def _maybe_drop_privileges(worker_uid, worker_gid, *, drop=_real_drop, geteuid=os.geteuid) -> None:
    """In-child decision: drop to the worker uid/gid iff configured AND privileged.

    - worker_uid None (unconfigured / dev): no isolation requested → no-op.
    - configured but euid != 0 (can't drop): warns + dev-mode no-op (never crash). The
      warning fires per fork by design — it only happens when isolation was REQUESTED
      but the process is unprivileged (a loud, repeated misconfig signal); a once-flag
      wouldn't dedupe across forked worker processes anyway (each child starts fresh).
    - configured AND euid 0: drop(worker_uid, worker_gid); a raised drop PROPAGATES
      so the requested-but-undropped turn never runs (fail-closed, AD-6).

    A configured uid REQUIRES a configured gid — a uid without a gid is fail-closed
    (raise) rather than calling drop(uid, None), which would TypeError and be swallowed
    by the child's os._exit.
    """
    if worker_uid is None:
        return
    if geteuid() != 0:
        log.warning("running workers same-uid; vault isolation OFF — dev mode")
        return
    if worker_gid is None:
        raise RuntimeError("worker_uid configured without worker_gid; refusing to drop (fail-closed)")
    drop(worker_uid, worker_gid)


async def _os_fork_spawn(socket_path: str, turn_id: str, prompt: str,
                         *, worker_uid=None, worker_gid=None, drop=_real_drop,
                         memory_root=None, history_path=None):
    """Real fork seam: fork one worker that runs the turn then _exit()s. Linux-only
    in practice (macOS aborts fork-without-exec).

    The child drops privilege (AD-6) BEFORE running the turn; the privileged
    parent never elevates. A required-but-failed drop raises inside the try, so
    the `finally: os._exit(0)` exits the child before any turn runs (fail-closed).
    `memory_root`/`history_path` are the read-only roots the worker assembles its
    prompt from (Story 4.4).
    """
    from shelldon.worker.tools import build_tool_registry
    from shelldon.worker.worker import run_worker

    try:
        pid = os.fork()
    except OSError as exc:
        # The kernel refused the fork (ENOMEM / EAGAIN). Don't let a raw errno escape —
        # raise with context so spawn_turn → core's spawn-failure handler releases the
        # guards and the turn fails cleanly instead of crashing the loop (Story 5.0).
        raise RuntimeError(f"os.fork() failed for turn {turn_id}: {exc}") from exc
    if pid == 0:  # child
        code = 0
        try:
            # SQLite is not fork-safe: discard core's inherited WAL connection FIRST, while its
            # fds are still valid, so the closerange below can't leave a torn -shm mmap that
            # breaks the worker's own read-only history open (SQLITE_PROTOCOL on the Pi).
            _close_inherited_sqlite()
            # Fork-without-exec hygiene: close FDs inherited from the parent (the bus
            # listener, peer sockets, etc.) BEFORE doing anything — the worker opens its
            # own fresh connection. Keep std streams (0,1,2) so failures stay visible.
            # Cap the range (SC_OPEN_MAX can be ~1e6) — only low FDs are ever inherited.
            os.closerange(3, min(os.sysconf("SC_OPEN_MAX"), _MAX_INHERITED_FD))
            _maybe_drop_privileges(worker_uid, worker_gid, drop=drop)
            asyncio.run(run_worker(socket_path, turn_id, prompt,
                                   memory_root=memory_root, history_path=history_path,
                                   tool_registry=build_tool_registry()))
        except BaseException:
            # A failed drop/turn must NOT vanish as exit 0 — exit non-zero so the reaper
            # (and a human reading exit status) can SEE the child failed (Story 5.0).
            code = 1
        finally:
            os._exit(code)  # reclaim RAM; never run parent teardown in the child
    return pid


async def _os_waitpid_reap(handle, *, waitpid=os.waitpid, kill=os.kill,
                           timeout=_REAP_TIMEOUT_S, poll=_REAP_POLL_S) -> None:
    """Reap the worker without threads (thread-free keeps future forks safe).

    Bounded (Story 5.0): if the child hasn't exited within `timeout`, escalate to SIGKILL
    and reclaim it with a blocking waitpid — an unkillable/wedged child can never spin
    this loop forever or hold the ≤1 slot. A child that exited abnormally (signalled or
    non-zero) is logged so the failure is visible. The os calls are injected for tests."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            pid, status = waitpid(handle, os.WNOHANG)
        except ChildProcessError:
            return  # already reaped (e.g. by a SIGCHLD handler) — benign
        if pid != 0:
            if os.WIFSIGNALED(status) or (os.WIFEXITED(status) and os.waitstatus_to_exitcode(status)):
                log.warning("worker %s exited abnormally (status=%s)", handle, status)
            return
        if loop.time() >= deadline:
            log.warning("worker %s did not exit in %.1fs; escalating to SIGKILL", handle, timeout)
            try:
                kill(handle, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass  # already gone between the poll and the kill — benign
            # Reclaim the zombie by POLLING, not a blocking waitpid: a child wedged in
            # uninterruptible (D-state) kernel sleep wouldn't reap even on SIGKILL, and a
            # blocking waitpid here would freeze the whole asyncio loop. Poll briefly,
            # then give up — the OS reaper collects a leftover zombie eventually.
            kill_deadline = loop.time() + _REAP_KILL_GRACE_S
            while loop.time() < kill_deadline:
                try:
                    pid, _ = waitpid(handle, os.WNOHANG)
                except ChildProcessError:
                    return  # reclaimed
                if pid != 0:
                    return  # reclaimed
                await asyncio.sleep(poll)
            log.warning("worker %s did not reap after SIGKILL; leaving the zombie to the OS", handle)
            return
        await asyncio.sleep(poll)


class ForkServer:
    """Warm-fork lifecycle: preload+freeze once, then one worker per turn."""

    def __init__(self, socket_path, *, spawn=None, reap=None,
                 preload_modules=(), manage_gc=True,
                 worker_uid=None, worker_gid=None, drop_privileges=None,
                 memory_root=None, history_path=None):
        self.socket_path = socket_path
        self._worker_uid = worker_uid
        self._worker_gid = worker_gid
        self._drop = drop_privileges or _real_drop
        #: Read-only roots the forked worker assembles its prompt from (Story 4.4).
        self._memory_root = memory_root
        self._history_path = history_path
        #: An explicit spawn= still wins (test seam); the default routes through
        #: _default_spawn so the configured uid/gid reach the fork child.
        self._spawn = spawn or self._default_spawn
        self._reap = reap or _os_waitpid_reap
        self._preload_modules = tuple(preload_modules)
        self._manage_gc = manage_gc
        self._ready = asyncio.Event()
        self._inflight = None
        #: Mechanical ≤1 bound — the spawner physically can't run two workers.
        #: core's Arbiter (the *policy* on when to request a turn) wires in at 1.8.
        self.worker_in_flight = False

    async def _default_spawn(self, socket_path, turn_id, prompt):
        """Default spawner: forks via _os_fork_spawn carrying the configured
        worker uid/gid + drop fn through to the child."""
        return await _os_fork_spawn(socket_path, turn_id, prompt,
                                    worker_uid=self._worker_uid,
                                    worker_gid=self._worker_gid,
                                    drop=self._drop,
                                    memory_root=self._memory_root,
                                    history_path=self._history_path)

    async def preload(self) -> None:
        """Warm the libs and freeze them COW-shared, then raise the readiness barrier.

        Ordering (binding, AD-3): gc.disable() → import → gc.collect() → gc.freeze().
        gc.freeze() exempts the pre-imported objects from GC scans so they stay
        shared after fork. (Refcount writes still dirty pages — the RAM win needs
        the worker not to deep-traverse shared objects.)
        """
        if self._manage_gc:
            gc.disable()
        try:
            for mod in self._preload_modules:
                importlib.import_module(mod)
            if self._manage_gc:
                gc.collect()
                gc.freeze()
        except BaseException:
            # ANY failure in the gc-managed warm-up (a bad import, or gc.collect/freeze
            # itself) must NOT leave the parent with GC permanently disabled — re-enable
            # before propagating so the parent stays healthy (Story 5.0). On SUCCESS, GC
            # stays disabled by design (COW: gc.freeze keeps shared pages clean, AD-3).
            if self._manage_gc:
                gc.enable()
            raise
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
