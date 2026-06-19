"""Story 4.3 — the production composition root (`shelldon/app.py`).

Two halves, both unprivileged + cross-platform: (1) config resolution + the vault
bootstrap + the dev-mode warning, and (2) an end-to-end turn through the injected
in-process launcher with a clean, orphan-free teardown. The real multi-process
launch + the real uid drop are Linux/Pi-exercised (see test_vault_isolation.py).
"""

import asyncio
import os
import stat

import pytest

from shelldon import app
from shelldon.app import resolve_worker_identity, run_app
from shelldon.display.renderer import StubRenderer
from shelldon.worker.forkserver import ForkServer
from shelldon.worker.worker import run_worker


# --- config resolution (resolve_worker_identity) ---


def test_unconfigured_is_dev_mode_none():
    assert resolve_worker_identity({}) == (None, None)


def test_uid_gid_from_env():
    assert resolve_worker_identity({"SHELLDON_WORKER_UID": "1500", "SHELLDON_WORKER_GID": "1600"}) == (1500, 1600)


def test_uid_without_gid_fails_fast():
    with pytest.raises(RuntimeError, match="without SHELLDON_WORKER_GID"):
        resolve_worker_identity({"SHELLDON_WORKER_UID": "1500"})


def test_non_integer_uid_rejected():
    with pytest.raises(RuntimeError, match="invalid SHELLDON_WORKER_UID"):
        resolve_worker_identity({"SHELLDON_WORKER_UID": "nobody", "SHELLDON_WORKER_GID": "1600"})


@pytest.mark.skipif(os.getuid() == 0, reason="current user is root → would hit the uid-0 reject")
def test_worker_user_resolved_via_pwd():
    # The current user always exists; assert it resolves to that account's real ids.
    import pwd

    pw = pwd.getpwuid(os.getuid())
    assert resolve_worker_identity({"SHELLDON_WORKER_USER": pw.pw_name}) == (pw.pw_uid, pw.pw_gid)


def test_unknown_worker_user_fails_fast():
    with pytest.raises(RuntimeError, match="not found"):
        resolve_worker_identity({"SHELLDON_WORKER_USER": "no-such-user-shd-4-3"})


def test_uid_zero_root_rejected():
    # Running workers as root would defeat the OS isolation entirely (review finding).
    with pytest.raises(RuntimeError, match="root"):
        resolve_worker_identity({"SHELLDON_WORKER_UID": "0", "SHELLDON_WORKER_GID": "0"})


def test_gid_without_uid_fails_fast():
    # A lone GID is almost certainly an operator typo — fail fast, don't silently ignore.
    with pytest.raises(RuntimeError, match="without SHELLDON_WORKER_UID"):
        resolve_worker_identity({"SHELLDON_WORKER_GID": "1600"})


# --- in-process spawn seam (the harness pattern from test_end_to_end_turn) ---


class _Source:
    """A controllable inbound line source: `feed()` queues a line."""

    def __init__(self):
        self._q: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, line: str) -> None:
        self._q.put_nowait(line)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


class OkProvider:
    name = "fake"

    async def complete(self, prompt: str) -> str:
        return f"reply to: {prompt}"


async def _await(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# --- the smoke: vault bootstrap + dev warning + end-to-end turn + clean teardown ---


async def test_app_root_smoke_turn_and_clean_teardown(sock_path, tmp_path, caplog):
    memory_root = tmp_path / "memory"
    source = _Source()
    outbound: list[str] = []

    async def sink(text: str) -> None:
        outbound.append(text)

    # Inject the in-process fork seam so no real os.fork() (macOS-safe), and a fake
    # provider so there is no network. run_app still does the real vault bootstrap,
    # the real config resolution, and the dev-mode warning. `spawned` records each
    # worker task so teardown can prove none is left orphaned.
    spawned: list[asyncio.Task] = []

    def _spawn(sp, tid, p):
        # Identity assembly (Story 4.4) — the smoke asserts the echoed raw message,
        # not the assembled prompt (assembly has its own tests).
        task = asyncio.create_task(run_worker(sp, tid, p, assemble=lambda m: m))
        spawned.append(task)
        return task

    fs = ForkServer(sock_path, spawn=_spawn, reap=lambda h: h, manage_gc=False)

    with caplog.at_level("WARNING", logger="shelldon.app"):
        app_task = asyncio.ensure_future(
            run_app(
                socket_path=sock_path,
                memory_root=memory_root,
                env={},  # unconfigured → dev mode
                chain=[OkProvider()],
                renderer=StubRenderer(),
                inbound=source,
                outbound=sink,
                forkserver=fs,
                launch_actors=app.launch_in_process,
                core_kwargs={"reflex_interval": 3600, "turn_timeout": 5.0},
            )
        )
        try:
            # (a) vault/ was created owner-only by the service process.
            await _await(lambda: (memory_root / "vault").is_dir())
            mode = stat.S_IMODE(os.stat(memory_root / "vault").st_mode)
            assert mode == 0o700

            # (b) a full turn completes end-to-end through the composed actors.
            source.feed("hello pet")
            await _await(lambda: outbound == ["reply to: hello pet"])

            # (c) the dev-mode "isolation OFF" warning was emitted (no uid configured).
            assert any("vault isolation OFF" in r.message for r in caplog.records)
        finally:
            app_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await app_task

    # (d) clean teardown — every worker was reaped (no orphans), the ≤1 guard is
    # released, and the bus stopped cleanly (its socket is unlinked).
    assert spawned and all(task.done() for task in spawned)
    assert fs.worker_in_flight is False
    assert not os.path.exists(sock_path)
