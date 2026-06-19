"""Story 5.0 — resilience hardening: the turn lifecycle never wedges.

Failure-branch coverage (the whole point of this story): the ≤1 slot always releases,
timeouts are coherent (W < R < T), the outbound write is bounded, and the fork/reap/
preload path is robust. Cross-platform — the real os.fork() child path stays Linux-gated
in test_forkserver_fork.py; here the OS calls are injected.
"""

import asyncio
import gc
import logging
import os
import signal

import pytest

from shelldon.contracts import Actor, Envelope, MsgKind, Result
from shelldon.core.runtime import DEFAULT_TURN_TIMEOUT, Core
from shelldon.worker.forkserver import ForkServer, _os_fork_spawn, _os_waitpid_reap
from shelldon.worker.worker import _COMPLETION_TIMEOUT_S, _RESULT_WRITE_TIMEOUT_S


class _IdleSpawner:
    """A spawner whose turn methods are never driven (these tests poke Core internals)."""

    async def ready(self):  # pragma: no cover - run() is never called here
        pass


def _result_env(turn_id, *, ok=True, payload="x"):
    return Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=ok, payload=payload), turn_id=turn_id,
    )


def _open_turn(core, turn_id="t1", prompt="hi"):
    """Reserve a turn the way the core loop would (arbiter + fence + stash)."""
    assert core.arbiter.submit(prompt) == prompt
    core.fence.open(turn_id)
    core._current_turn_id = turn_id
    core._current_prompt = prompt


# --- AC2: the coherent-timeout invariant W < R < T ---


def test_timeout_chain_is_coherent():
    """The wedge existed because the worker timeout (120s) was 4x core's degrade (30s).
    The fix is a strict ordering: worker self-report < reap SIGKILL < core degrade."""
    from shelldon.worker.forkserver import _REAP_TIMEOUT_S

    assert _COMPLETION_TIMEOUT_S < _REAP_TIMEOUT_S < DEFAULT_TURN_TIMEOUT
    # The outbound write must give up well before the broker-read window — strictly less,
    # so a reply landing near the read deadline isn't cut off mid-write by the reaper.
    assert _RESULT_WRITE_TIMEOUT_S < _COMPLETION_TIMEOUT_S


# --- AC1: the ≤1 slot ALWAYS releases, even when delivery raises ---


async def test_handle_result_releases_slot_when_reply_send_raises(sock_path):
    core = Core(sock_path, _IdleSpawner())
    try:
        _open_turn(core)

        async def boom(_text):
            raise OSError("transport down")

        core._send_reply = boom  # bus.deliver fails on the reply

        await core._handle_result(_result_env("t1"))

        assert core.arbiter.is_idle  # slot released despite the failed delivery
        assert core.fence.is_idle
    finally:
        core.history.close()


async def test_timeout_path_releases_slot_when_degrade_raises(sock_path):
    core = Core(sock_path, _IdleSpawner(), turn_timeout=0.01)
    try:
        _open_turn(core)

        async def boom(_text):
            raise OSError("transport down")

        core._send_reply = boom  # _degrade()'s reply send fails

        await core._timeout_watch("t1")

        assert core.arbiter.is_idle  # slot released even though the degrade reply failed
    finally:
        core.history.close()


async def test_handle_result_releases_slot_when_degrade_raises(sock_path):
    """The failure Result (ok=False) branch: even if the degrade ack fails to send, the
    slot still releases — the else-branch failure path, not just the ok-branch."""
    core = Core(sock_path, _IdleSpawner())
    try:
        _open_turn(core)

        async def boom(_text):
            raise OSError("transport down")

        core._send_reply = boom  # _degrade() -> _send_reply raises

        await core._handle_result(_result_env("t1", ok=False, payload=""))

        assert core.arbiter.is_idle
        assert core.fence.is_idle
    finally:
        core.history.close()


async def test_handle_result_drives_catch_up_even_after_a_failed_reply(sock_path):
    """The guarantee is end-to-end: a failed reply must still flush the pending
    catch-up turn (arbiter.complete must run), not strand it forever."""
    core = Core(sock_path, _IdleSpawner())
    try:
        _open_turn(core, "t1", "first")
        core.arbiter.submit("second")  # coalesced while t1 in flight

        started = []

        async def fake_start(prompt):
            started.append(prompt)

        async def boom(_text):
            raise OSError("down")

        core._send_reply = boom
        core._start_turn = fake_start

        await core._handle_result(_result_env("t1"))

        assert started == ["second"]  # catch-up still fired despite the failed reply
    finally:
        core.history.close()


# --- AC1: the reap is sequenced BEFORE the arbiter releases (no divergence window) ---


async def test_reap_runs_before_the_catch_up_spawn(sock_path):
    """The structural divergence fix: a turn end must reclaim the worker (fork-server
    guard released) BEFORE the arbiter frees its slot and drives the catch-up — otherwise
    the catch-up spawn could hit a still-held fork (WorkerBusyError) and drop the turn."""
    order = []

    class OrderSpawner:
        async def ready(self):  # pragma: no cover
            pass

        async def spawn_turn(self, turn_id, prompt):
            order.append(("spawn", prompt))

        async def reap_current(self):
            order.append("reap")

    core = Core(sock_path, OrderSpawner())
    try:
        async def noop(*_a):
            pass

        core._push_face = noop
        core._send_reply = noop

        assert core.arbiter.submit("first") == "first"  # reserve the slot like run()'s loop
        await core._start_turn("first")          # spawns "first", schedules its reap task
        turn_id = core._current_turn_id
        assert core.arbiter.submit("second") is None    # coalesced while "first" in flight

        await core._handle_result(_result_env(turn_id))

        # reap MUST land between the two spawns — fork-server guard freed before the
        # arbiter drove the catch-up spawn of "second".
        assert order == [("spawn", "first"), "reap", ("spawn", "second")]
    finally:
        core.history.close()


# --- AC4: runtime releases the arbiter slot on a real spawn failure ---


async def test_start_turn_releases_arbiter_on_spawn_failure(sock_path):
    class FailSpawner:
        async def ready(self):  # pragma: no cover
            pass

        async def spawn_turn(self, turn_id, prompt):
            raise RuntimeError("os.fork() failed: ENOMEM")

        async def reap_current(self):  # pragma: no cover
            pass

    core = Core(sock_path, FailSpawner())
    try:
        assert core.arbiter.submit("hi") == "hi"

        async def noop(_face):
            pass

        core._push_face = noop  # avoid needing a running bus

        await core._start_turn("hi")

        assert core.arbiter.is_idle  # reset() released the admitted-but-unstarted slot
        assert core.fence.is_idle
    finally:
        core.history.close()


# --- AC2: the outbound Result write is bounded ---


async def test_outbound_result_write_is_bounded(monkeypatch):
    import shelldon.worker.worker as w

    monkeypatch.setattr(w, "_RESULT_WRITE_TIMEOUT_S", 0.05)

    class _FakeWriter:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_connect(socket_path, actor):
        return object(), _FakeWriter()

    async def fake_result(reader, turn_id):
        return Result(ok=True, payload="reply")

    async def fake_write_frame(writer, env):
        if env.kind is MsgKind.RESULT:
            await asyncio.sleep(10)  # core stopped reading — the write would hang forever
        # the JOB write returns immediately

    monkeypatch.setattr(w, "connect", fake_connect)
    monkeypatch.setattr(w, "_result_from_broker", fake_result)
    monkeypatch.setattr(w, "write_frame", fake_write_frame)

    # Without the bound this hangs 10s; with it, run_worker returns cleanly (no raise).
    completed = False
    await asyncio.wait_for(
        w.run_worker("/tmp/x.sock", "t1", "hi", assemble=lambda m: m), timeout=2.0
    )
    completed = True
    assert completed  # the timeout is swallowed → the worker exits normally, never raises


# --- AC3: bounded reap with SIGKILL escalation ---


async def test_reap_escalates_to_sigkill_for_a_stuck_child():
    killed = []
    state = {"alive": True}

    def fake_waitpid(pid, flags):
        if flags == os.WNOHANG:
            return (0, 0) if state["alive"] else (pid, 0)
        return (pid, 0)  # blocking reclaim after the kill

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    await _os_waitpid_reap(4242, waitpid=fake_waitpid, kill=fake_kill, timeout=0.05, poll=0.005)

    assert killed == [(4242, signal.SIGKILL)]  # an unkillable child gets SIGKILL'd, not spun forever


async def test_reap_returns_on_natural_exit_without_kill():
    killed = []

    def fake_waitpid(pid, flags):
        return (pid, 0)  # already exited cleanly

    await _os_waitpid_reap(7, waitpid=fake_waitpid, kill=lambda *a: killed.append(a), timeout=0.05)

    assert killed == []  # a child that exits on its own is never SIGKILL'd


async def test_reap_logs_abnormal_child_exit(caplog):
    def fake_waitpid(pid, flags):
        return (pid, 1 << 8)  # WIFEXITED with code 1

    with caplog.at_level(logging.WARNING):
        await _os_waitpid_reap(9, waitpid=fake_waitpid, kill=lambda *a: None)

    assert "exited abnormally" in caplog.text  # the failure is visible, not silent


# --- AC3: fork OSError surfaces as a clean failure, not a raw errno crash ---


async def test_fork_oserror_becomes_runtime_error(monkeypatch):
    def boom():
        raise OSError("EAGAIN: resource temporarily unavailable")

    monkeypatch.setattr(os, "fork", boom)

    with pytest.raises(RuntimeError, match=r"os\.fork\(\) failed"):
        await _os_fork_spawn("/tmp/x.sock", "t1", "hi")


# --- AC3: preload re-enables GC if a module import fails ---


async def test_preload_reenables_gc_on_import_failure():
    fs = ForkServer("/tmp/x.sock", preload_modules=("shelldon._does_not_exist_zzz",), manage_gc=True)
    was_enabled = gc.isenabled()
    try:
        with pytest.raises(ModuleNotFoundError):
            await fs.preload()
        assert gc.isenabled()  # NOT left disabled in the parent
    finally:
        # restore whatever the suite started with
        gc.enable() if was_enabled else gc.disable()
