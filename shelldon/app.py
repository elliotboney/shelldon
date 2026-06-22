"""Production composition root (Story 4.3): `python -m shelldon`.

Wires the five actors around one bus, creates the memory tree incl. the OS-locked
`vault/`, and configures the fork-server with the worker uid/gid so each forked
worker drops privilege before running its turn (AD-6). Real OS isolation needs
Linux + privilege; on an unprivileged/dev box the drop is a logged no-op (same-uid)
and the wiring is otherwise identical.

The actor launch is injected behind a `launch_actors` seam (the same "ship the
mechanism + seam, gate the heavyweight path" discipline as Story 1.5's fork seam):
production forks real OS processes — core (owns the bus + fork-server, service uid)
in the main process, broker/display/transport as `multiprocessing` children
(AD-2) — while the test smoke injects the in-process asyncio-task launcher, so the
composition + lifecycle is exercised cross-platform and unprivileged. This is
ADDITIVE: `Core`, `run_broker`, and `ForkServer` are composed unchanged.
"""

import asyncio
import logging
import os
import pwd
import signal

from shelldon.broker.chain import build_chain
from shelldon.broker.service import run_broker
from shelldon.core.bus.server import bus_socket_path
from shelldon.core.memory import DEFAULT_MEMORY_ROOT
from shelldon.core.runtime import Core
from shelldon.core.vault import ensure_vault
from shelldon.core.selfcode import DEFAULT_WORKSPACE_ROOT, live_tools_dir, staging_dir
from shelldon.display.renderer import StubRenderer
from shelldon.display.service import run_display
from shelldon.plugins.host import run_plugin_host
from shelldon.transport.cli import run_cli_transport
from shelldon.worker.forkserver import ForkServer

log = logging.getLogger("shelldon.app")

#: How long to wait for a launched child process to exit on teardown before moving
#: on (it was already terminate()d — this just bounds the join so a wedged child
#: can't hang shutdown forever).
_CHILD_JOIN_TIMEOUT_S = 5.0


def resolve_worker_identity(env=None) -> tuple[int | None, int | None]:
    """Resolve the worker drop target `(uid, gid)` from the environment, or
    `(None, None)` when unconfigured (dev mode — no isolation requested).

    `SHELLDON_WORKER_USER` (resolved via `pwd`) wins; else `SHELLDON_WORKER_UID`
    **plus** `SHELLDON_WORKER_GID` (both required — a uid without a gid, OR a gid
    without a uid, is a misconfig that must fail fast, never start silently
    half-configured). uid 0 (root) is rejected — running workers as root defeats the
    isolation entirely. A bad user / non-integer id raises `RuntimeError`.
    """
    env = os.environ if env is None else env
    user = env.get("SHELLDON_WORKER_USER")
    if user:
        try:
            pw = pwd.getpwnam(user)
        except KeyError as exc:
            raise RuntimeError(f"SHELLDON_WORKER_USER={user!r} not found") from exc
        if pw.pw_uid == 0:
            raise RuntimeError(f"SHELLDON_WORKER_USER={user!r} resolves to uid 0 (root) — that defeats isolation")
        return pw.pw_uid, pw.pw_gid
    uid_s = env.get("SHELLDON_WORKER_UID")
    gid_s = env.get("SHELLDON_WORKER_GID")
    if uid_s is None:
        if gid_s is not None:
            raise RuntimeError("SHELLDON_WORKER_GID is set without SHELLDON_WORKER_UID")
        return None, None  # unconfigured — dev mode
    if gid_s is None:
        raise RuntimeError("SHELLDON_WORKER_UID is set without SHELLDON_WORKER_GID")
    try:
        uid, gid = int(uid_s), int(gid_s)
    except ValueError as exc:
        raise RuntimeError(f"invalid SHELLDON_WORKER_UID/SHELLDON_WORKER_GID: {exc}") from exc
    if uid == 0:
        raise RuntimeError("SHELLDON_WORKER_UID=0 (root) defeats isolation — refusing")
    return uid, gid


async def _transport_actor(socket_path, inbound=None, outbound=None, env=None) -> None:
    """The chat surface: the Telegram bot when `SHELLDON_TRANSPORT=telegram` (build from
    `TELEGRAM_BOT_TOKEN`/`ALLOWED_USERS`), else the local CLI (stdin/stdout, or the injected
    in/out the test smoke passes). Gated like the renderer — the heavyweight path engages
    only when configured. `telegram` is lazy-imported so it (and httpx) load only when used."""
    env = os.environ if env is None else env
    if env.get("SHELLDON_TRANSPORT", "").strip().lower() == "telegram":
        from shelldon.transport.telegram import run_telegram_from_env

        await run_telegram_from_env(socket_path, env)
    else:
        await run_cli_transport(socket_path, inbound=inbound, outbound=outbound)


def _default_renderer(env):
    """The display surface: the real Waveshare E-Ink panel when `SHELLDON_DISPLAY=waveshare`
    (the Pi), else the recording `StubRenderer` (dev/headless — no hardware touched). Gated
    like the worker uid-drop: the heavyweight path engages only when explicitly configured.
    `WaveshareRenderer` imports cleanly here (PIL/spidev/driver are lazy-imported on first
    draw), so referencing it off-Pi is safe."""
    if env.get("SHELLDON_DISPLAY", "").strip().lower() == "waveshare":
        from shelldon.display.waveshare import WaveshareRenderer

        return WaveshareRenderer()
    return StubRenderer()


async def _await_bus_up(core: Core, timeout: float = 5.0) -> None:
    """Block until core's bus is listening, so children can connect (an unregistered
    or unreachable destination drops/fails — children must start after the socket).

    Polls `core.bus._server` — accepted internal coupling (the in-process test harness
    uses the same idiom). A public `BusServer` readiness API would be cleaner; deferred
    as a nice-to-have since this whole launcher is Pi/Linux-deploy-verified (review 4.3).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if core.bus._server is not None:
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("bus did not come up in time")


async def launch_in_process(core, socket_path, chain, renderer, inbound, outbound) -> None:
    """Dev/test launcher: the actors as asyncio tasks on one event loop (the proven
    in-process harness). On cancellation it cancels every task and stops the bus —
    no orphaned workers. Production uses `launch_multiprocess`."""
    tasks = [
        asyncio.create_task(core.run()),
        asyncio.create_task(run_broker(socket_path, chain)),
        asyncio.create_task(run_display(socket_path, renderer)),
        asyncio.create_task(_transport_actor(socket_path, inbound, outbound)),
        asyncio.create_task(run_plugin_host(socket_path)),  # Story 7.1 — empty set, idles
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await core.bus.stop()


def _broker_proc(socket_path, env) -> None:  # pragma: no cover - runs in a child process (Linux/deploy)
    asyncio.run(run_broker(socket_path, build_chain(env)))


def _display_proc(socket_path) -> None:  # pragma: no cover - runs in a child process
    # The display child builds its renderer from env (like the broker child builds its chain):
    # the real Waveshare panel on the Pi (SHELLDON_DISPLAY=waveshare), else the stub.
    asyncio.run(run_display(socket_path, _default_renderer(os.environ)))


def _transport_proc(socket_path) -> None:  # pragma: no cover - runs in a child process
    asyncio.run(_transport_actor(socket_path, env=os.environ))  # telegram or CLI, per env


def _plugin_host_proc(socket_path) -> None:  # pragma: no cover - runs in a child process
    asyncio.run(run_plugin_host(socket_path))  # Story 7.1 — loads plugins/ (empty in 7.1)


async def launch_multiprocess(core, socket_path, chain, renderer, inbound, outbound) -> None:  # pragma: no cover - real multi-process launch is exercised on Linux/Pi
    """Production launcher (AC3): core (+ bus + fork-server) in this service process,
    broker/display/transport as real OS processes. Real two-uid isolation only bites
    here on Linux + privilege — exercised on the Pi, like the Linux-gated real-fork
    test. On cancellation it terminates + joins the children (no orphans) and stops
    the bus. The injected `chain`/`renderer`/`inbound`/`outbound` are the in-process
    fakes; the production children build their own from env/stdio."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    core_task = asyncio.create_task(core.run())
    await _await_bus_up(core)
    children = [
        ctx.Process(target=_broker_proc, args=(socket_path, dict(os.environ)), name="shelldon-broker"),
        ctx.Process(target=_display_proc, args=(socket_path,), name="shelldon-display"),
        ctx.Process(target=_transport_proc, args=(socket_path,), name="shelldon-transport"),
        ctx.Process(target=_plugin_host_proc, args=(socket_path,), name="shelldon-plugin-host"),
    ]
    for child in children:
        child.start()
    try:
        await core_task
    finally:
        core_task.cancel()
        for child in children:
            if child.is_alive():
                child.terminate()
        for child in children:
            child.join(timeout=_CHILD_JOIN_TIMEOUT_S)
        await core.bus.stop()


async def run_app(
    *,
    socket_path=None,
    memory_root=None,
    env=None,
    chain=None,
    renderer=None,
    inbound=None,
    outbound=None,
    forkserver=None,
    launch_actors=None,
    core_kwargs=None,
) -> None:
    """Compose + run the whole pet: create the memory tree incl. the OS-locked vault,
    resolve the worker privilege-drop config, build the fork-server, and launch the
    actors. Every heavyweight piece is injectable so the smoke test runs it in-process,
    unprivileged, cross-platform; production injects nothing and gets the real model."""
    env = os.environ if env is None else env
    socket_path = socket_path or bus_socket_path()
    memory_root = memory_root if memory_root is not None else DEFAULT_MEMORY_ROOT

    # Resolve the privilege-drop config FIRST: a misconfig (bad uid/gid, root, half-set)
    # must raise before any filesystem side effect (the vault dir).
    worker_uid, worker_gid = resolve_worker_identity(env)
    if worker_uid is None:
        log.warning("no worker uid configured; running workers same-uid — vault isolation OFF (dev mode)")

    # The vault is created owner-only (0700) by this service process, BEFORE any
    # worker can fork — so the dropped worker uid is OS-denied from the first turn.
    ensure_vault(memory_root)

    # The FREE-tier file tools (Story 9.2) are jailed to this workspace; create it with
    # NORMAL perms (NOT 0700 like the vault) so the dropped worker uid can read it. Story 9.4
    # adds the live + staging tool dirs under it: core stages/promotes self-coded tools and the
    # worker imports the live dir at each fork's build_tool_registry.
    os.makedirs(DEFAULT_WORKSPACE_ROOT, exist_ok=True)
    os.makedirs(live_tools_dir(DEFAULT_WORKSPACE_ROOT), exist_ok=True)
    os.makedirs(staging_dir(DEFAULT_WORKSPACE_ROOT), exist_ok=True)

    if forkserver is None:
        # The forked worker assembles its prompt (Story 4.4) from the SAME memory_root
        # core writes; history_path defaults (None) to the same DEFAULT_HISTORY_PATH core uses.
        forkserver = ForkServer(
            socket_path, worker_uid=worker_uid, worker_gid=worker_gid, memory_root=memory_root
        )
    await forkserver.preload()

    core = Core(socket_path, forkserver, memory_root=memory_root, **(core_kwargs or {}))
    chain = chain if chain is not None else build_chain(env)
    renderer = renderer if renderer is not None else _default_renderer(env)
    launch = launch_actors or launch_multiprocess
    await launch(core, socket_path, chain, renderer, inbound, outbound)


async def _amain() -> None:  # pragma: no cover - the real process entrypoint
    task = asyncio.ensure_future(run_app())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:
            pass  # signal handlers unavailable (non-main-thread / non-unix)
    try:
        await task
    except asyncio.CancelledError:
        log.info("shelldon shut down cleanly")


def main() -> None:  # pragma: no cover - the real process entrypoint
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
