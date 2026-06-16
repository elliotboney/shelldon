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

from shelldon.contracts import ROUTING_TABLE, Actor, Envelope
from shelldon.core.bus.frame import read_frame, write_frame

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
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        # start_unix_server fails if the path exists — clear any stale socket.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)

    async def stop(self) -> None:
        # Force-close active connections first: Server.wait_closed() (3.13) blocks
        # until open connections finish, and handlers idle in read_frame until EOF.
        writers = list(self._conns)
        for writer in writers:
            writer.close()
        self._conns.clear()
        self._registry.clear()
        for writer in writers:
            try:
                await writer.wait_closed()  # let the transport finish draining
            except Exception:
                pass
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
        self._conns.add(writer)
        try:
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
                actor = env.src
                self._registry[env.src] = writer  # lazy-src registration
                await self._route(env)
        finally:
            if actor is not None and self._registry.get(actor) is writer:
                del self._registry[actor]
            self._conns.discard(writer)
            writer.close()

    async def _route(self, env: Envelope) -> None:
        dest = ROUTING_TABLE[env.kind]
        if dest is Actor.CORE:
            await self.core_inbox.put(env)
            return
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
