"""core.bus — the Envelope bus hub (AD-4) and its length-prefixed UDS framing."""

from shelldon.core.bus.frame import (
    MAX_FRAME_BYTES,
    connect,
    read_frame,
    write_frame,
)
from shelldon.core.bus.server import BusServer, bus_socket_path

__all__ = [
    "BusServer",
    "bus_socket_path",
    "connect",
    "read_frame",
    "write_frame",
    "MAX_FRAME_BYTES",
]
