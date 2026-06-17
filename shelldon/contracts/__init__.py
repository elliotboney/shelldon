"""contracts — versioned msgspec Envelope/Job/Result with a closed header.

The one wire vocabulary every process shares (AD-4). Types are versioned and
round-trip-tested from M0 (AD-10); the header is closed (AD-11) and carries no
credentials (AD-2/NFR9). The UDS transport, length-prefix framing, and the
kind->destination routing table are Story 1.3 — this module stops at the typed
structs and their msgpack encode/decode.
"""

from enum import StrEnum

import msgspec

#: Schema version stamped onto every Envelope (AD-11 `v`). Bump on a breaking change.
SCHEMA_VERSION = 1


class Actor(StrEnum):
    """The addressable processes — the domain of an Envelope's `src`/`dst`."""

    CORE = "core"
    BROKER = "broker"
    WORKER = "worker"
    CHAT_TRANSPORT = "chat-transport"
    DISPLAY = "display"
    PLUGIN_HOST = "plugin-host"


class MsgKind(StrEnum):
    """Closed set of envelope kinds. Later stories extend this as they add kinds."""

    JOB = "job"
    RESULT = "result"


class Job(msgspec.Struct, frozen=True, tag="job", forbid_unknown_fields=True):
    """A request body. Minimal contract shell — broker/worker stories (1.4/1.5)
    define the real payload. Carries NO credentials: the broker injects creds
    internally (AD-2), so nothing credential-shaped may ever appear here.
    """

    payload: str


class Result(msgspec.Struct, frozen=True, tag="result", forbid_unknown_fields=True):
    """An outcome body, including the error variant — failures surface as a
    Result, never as an exception across the bus (Consistency Conventions).
    """

    ok: bool
    payload: str = ""
    error: str | None = None


#: Body type -> the header `kind` it must travel under (single source of truth
#: for the kind<->body agreement enforced in Envelope.__post_init__).
_KIND_FOR_BODY = {Job: MsgKind.JOB, Result: MsgKind.RESULT}


class Envelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The wire message: the closed header (AD-11) wrapping a typed body.

    `body` is a tagged union so the hub (Story 1.3) can decode polymorphically by
    tag. `dst=None` is reserved for the broadcast/subscription mode (AD-11), used
    later; point-to-point is the only mode 1.2 needs. `turn_id` exists from M0 so
    core can fence on it (AD-12), though the fencing logic lives in core.

    The header is CLOSED in two senses: `forbid_unknown_fields` rejects any field
    not in the schema, and `__post_init__` rejects a `kind` that disagrees with the
    body — so the header `kind` (what the hub routes on) can never drift from the
    body's own tag.
    """

    id: str
    kind: MsgKind
    src: Actor
    dst: Actor | None
    body: Job | Result
    v: int = SCHEMA_VERSION
    turn_id: str | None = None

    def __post_init__(self) -> None:
        expected = _KIND_FOR_BODY[type(self.body)]
        if self.kind != expected:
            raise ValueError(
                f"Envelope kind {self.kind!r} disagrees with body "
                f"{type(self.body).__name__} (expected kind {expected!r})"
            )


#: Static point-to-point routing table (AD-11 mode 1): the hub forwards an
#: envelope to the destination its `kind` maps to. Every MsgKind must have an
#: entry (enforced by test). Later stories add rows as they introduce kinds.
ROUTING_TABLE: dict[MsgKind, Actor] = {
    MsgKind.JOB: Actor.BROKER,
    MsgKind.RESULT: Actor.CORE,
}


_decoder = msgspec.msgpack.Decoder(Envelope)


def encode(env: Envelope) -> bytes:
    """Encode an Envelope to msgpack bytes (the bus wire format)."""
    return msgspec.msgpack.encode(env)


def decode(raw: bytes) -> Envelope:
    """Decode msgpack bytes back into a typed Envelope (resolves the body union).

    Rejects an envelope whose schema version `v` is not supported (AD-10) — a
    future/incompatible contract is a decode failure, not a silently-accepted message.
    """
    env = _decoder.decode(raw)
    if env.v != SCHEMA_VERSION:
        raise msgspec.ValidationError(
            f"unsupported schema version {env.v} (this build speaks v{SCHEMA_VERSION})"
        )
    return env


__all__ = [
    "SCHEMA_VERSION",
    "Actor",
    "MsgKind",
    "Job",
    "Result",
    "Envelope",
    "ROUTING_TABLE",
    "encode",
    "decode",
]
