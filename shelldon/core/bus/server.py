"""The bus hub (AD-4): a core-resident UDS server that routes Envelopes by the
static `kind`->destination table (AD-11 point-to-point mode).

Core is both the hub and a destination (`Actor.CORE`): CORE-bound envelopes go to
an in-process inbox queue, never over a socket. Every other actor is a remote
connection in the registry. A connection's identity is learned lazily from the
`src` of the frames it sends (sufficient for the JOB/RESULT senders in scope; a
registration handshake for pure-receiver actors is deferred to Story 1.7).
"""

import asyncio
import logging
import os
from pathlib import Path

import msgspec

from shelldon.contracts import ROUTING_TABLE, Actor, Envelope, MsgKind
from shelldon.core.bus.frame import read_frame, read_registration, write_frame

log = logging.getLogger("shelldon.bus")


def bus_socket_path() -> str:
    """Runtime default socket path (~/.shelldon/bus.sock), creating the dir."""
    base = Path.home() / ".shelldon"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "bus.sock")


class BusServer:
    """Hub-routing Envelope bus over a Unix domain socket."""

    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or bus_socket_path()
        self.core_inbox: asyncio.Queue[Envelope] = asyncio.Queue()
        self._registry: dict[Actor, asyncio.StreamWriter] = {}
        self._conns: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        # start_unix_server fails if the path exists — clear any stale socket.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)

    async def stop(self) -> None:
        # Drain every connection handler BEFORE closing the listening server. A silent
        # client parked in read_frame won't EOF just because we close its writer, and
        # Server.wait_closed() (3.13) blocks until all handler tasks finish — so we
        # CANCEL them. Loop because a just-accepted handler may register only after we
        # yield inside gather; cancelling each removes it (its finally discards from
        # `_handlers`), so the set drains to empty. Closing the server only once every
        # connection has detached avoids an asyncio Server._wakeup race under 3.13.
        while self._handlers:
            handlers = list(self._handlers)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        for writer in list(self._conns):
            writer.close()
        self._conns.clear()
        self._registry.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        actor: Actor | None = None
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self._conns.add(writer)
        try:
            # Every client announces its identity as the mandatory first frame, so
            # the hub can address receiver-first actors (e.g. the broker waiting on
            # Jobs). An unknown actor or EOF before registration drops the connection.
            try:
                actor = await read_registration(reader)
            except (msgspec.ValidationError, ValueError) as exc:
                log.warning("bad registration, closing connection: %s", exc)
                return
            if actor is None:
                return
            self._registry[actor] = writer

            while True:
                try:
                    env = await read_frame(reader)
                except msgspec.ValidationError as exc:
                    # Decodable framing, invalid message: the stream is still
                    # aligned, so skip this envelope and keep the connection.
                    log.warning("dropping invalid envelope: %s", exc)
                    continue
                except ValueError as exc:
                    # Framing error (oversized/corrupt length): the stream offset
                    # is lost — the connection is untrustworthy, so close it.
                    log.warning("framing error, closing connection: %s", exc)
                    break
                if env is None:  # clean EOF / peer gone (incl. mid-frame)
                    break
                await self._route(env)
        finally:
            if actor is not None and self._registry.get(actor) is writer:
                del self._registry[actor]
            self._conns.discard(writer)
            if task is not None:
                self._handlers.discard(task)
            writer.close()

    async def deliver(self, env: Envelope) -> None:
        """Emit a core-originated envelope onto the bus (AD-13/AD-5).

        Core is the hub AND a source: the runtime (Story 1.8) calls this to push
        OUTBOUND_MSG/STATE_SNAPSHOT outward. It routes identically to an inbound
        frame — a thin wrapper over `_route`, no new routing logic.
        """
        await self._route(env)

    async def _route(self, env: Envelope) -> None:
        if env.kind is MsgKind.EVENT:
            # AD-11 routing mode 2 (broadcast, Story 7.2): a pet-lifecycle event fans out
            # to the plugin-host, which dispatches it to the plugins that subscribed (the
            # manifest registry). One plugin-host today, so "fan out to N subscribers" is a
            # deliver-to-the-host; the host does the per-plugin fan-out. A broadcast with no
            # subscriber is NORMAL (the pet runs fine with zero plugins — CAP-3), so an
            # absent host is a debug-level drop, NOT the warning a missing point-to-point
            # target gets. (A registered host whose write then fails is abnormal — that
            # still warns + deregisters, in _deliver_to.)
            if Actor.PLUGIN_HOST in self._registry:
                await self._deliver_to(Actor.PLUGIN_HOST, env)
            else:
                log.debug("no plugin-host subscribed; dropping event %s", env.id)
            # Story 7.5: core is ALSO a broadcast consumer — a plugin-emitted affect nudge
            # (NUDGE_*) is reacted to in the runtime via core_inbox. Guarded by `src != CORE`
            # so core never receives its OWN broadcasts back (e.g. MESSAGE_ANSWERED): no
            # self-loop, no wasted enqueue. The hub stays kind-agnostic — the runtime's
            # reactions map decides which kinds actually move mood.
            if env.src is not Actor.CORE:
                await self.core_inbox.put(env)
            return
        dest = ROUTING_TABLE[env.kind]
        if dest is Actor.CORE:
            await self.core_inbox.put(env)
            return
        await self._deliver_to(dest, env)

    async def _deliver_to(self, dest: Actor, env: Envelope) -> None:
        """Write `env` to the registered `dest` connection; drop-with-log if `dest` is
        absent, and deregister it if the write fails — never kill the sender."""
        target = self._registry.get(dest)
        if target is None:
            log.warning("no connection for %s; dropping %s envelope %s", dest, env.kind, env.id)
            return
        try:
            await write_frame(target, env)
        except OSError as exc:
            # Target dropped between registration and this write — drop the
            # envelope and deregister the dead target; never kill the sender.
            log.warning("write to %s failed (%s); dropping %s and deregistering", dest, exc, env.id)
            if self._registry.get(dest) is target:
                del self._registry[dest]
