"""contracts — versioned msgspec Envelope/Job/Result with a closed header.

The one wire vocabulary every process shares (AD-4). Types are versioned and
round-trip-tested from M0 (AD-10); the header is closed (AD-11) and carries no
credentials (AD-2/NFR9). The UDS transport, length-prefix framing, and the
kind->destination routing table are Story 1.3 — this module stops at the typed
structs and their msgpack encode/decode.
"""

from enum import StrEnum
from typing import Literal

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
    COMPLETION = "completion"
    INBOUND_MSG = "inbound-message"
    OUTBOUND_MSG = "outbound-message"
    STATE_SNAPSHOT = "state-snapshot"
    EVENT = "event"


class Region(StrEnum):
    """Closed/registered display region ids (AD-5). The display is a compositor of
    regions, each with its own latest-wins snapshot stream; core owns the `face`
    region. A typo can't silently mint a new region — that's why this is an enum,
    not a free string. Plugin-claimed widget regions are added here in Epic 7.
    """

    FACE = "face"
    #: Plugin-claimed widget region (Story 7.1). Core owns FACE; a plugin may claim a
    #: widget region (e.g. the XP status-bar strip, Story 7.3) — the host rejects two
    #: plugins claiming the same region at load (AD-5: no two writers per region).
    STATUS_BAR = "status-bar"


class EventKind(StrEnum):
    """Closed set of broadcast `event` kinds (AD-11, Story 7.1). The second routing
    mode: the hub fans one of these out to every subscribed plugin (Story 7.2). The
    set is closed and declared HERE in `contracts/` — no component invents a kind, and
    the subscription registry is built at load from plugin manifests, not runtime
    self-registration. Story 7.1 only DECLARES this vocabulary (the `PluginManifest`
    subscribes against it); the `Event` wire body + hub fan-out land in Story 7.2, so
    this is intentionally NOT a `MsgKind`, has no `ROUTING_TABLE` row, and bumps no
    SCHEMA_VERSION (adding a new StrEnum is wire-additive).
    """

    MESSAGE_ANSWERED = "message-answered"
    TOOL_USED = "tool-used"
    DAY_ALIVE = "day-alive"
    #: Sensing kinds emitted BY plugins (Story 7.4) — the host validates a plugin only
    #: emits what its `manifest.emits` declares (AD-11). Same broadcast fan-out as above.
    BUTTON_PRESSED = "button-pressed"
    PRESENCE_ARRIVED = "presence-arrived"
    PRESENCE_LEFT = "presence-left"
    #: Generic AFFECT kinds (Story 7.5): a semantic mood nudge any plugin may emit. Distinct
    #: from the *fact* kinds above — a fact is "what happened" (a button was pressed), an
    #: affect is "how to feel" (get excited). Core (not the plugin) owns the magnitude: it
    #: maps each of these to a bounded, clamped mood patch via `core/reactions.py`. The
    #: sensing plugins (7.4) emit one of these ALONGSIDE their fact so the pet's face reacts.
    NUDGE_POSITIVE = "nudge-positive"
    NUDGE_NEGATIVE = "nudge-negative"
    NUDGE_EXCITED = "nudge-excited"
    NUDGE_CALM = "nudge-calm"


#: --- Memory-ops (AD-6): the closed, fixed-arg vocabulary core validates+applies ---
#: The three curated-memory ops, as frozen tagged structs with closed arg schemas —
#: "fixed arg schemas in contracts/, no free-text deltas" (AD-6). They are the shared
#: vocabulary core and the worker both speak: the worker proposes them on a `Result`
#: (Story 4.5) and core validates+applies them (sole writer, AD-5). `forbid_unknown_fields`
#: makes a typo'd field a decode error; the tags make a typo'd op (`remembr`) a decode error.


class Remember(msgspec.Struct, frozen=True, tag="remember", forbid_unknown_fields=True):
    """Record a fact or a person the owner mentioned, under the closed `collection`.

    `name` becomes a filename (core slugifies + path-guards it); `content` is the
    curated markdown body. `collection` is a closed Literal — a value outside the set
    is rejected by core on apply (msgspec only enforces it on decode, not on direct
    construction, so core re-validates)."""

    collection: Literal["facts", "people", "preferences", "capabilities"]
    name: str
    content: str


class RewriteAbout(msgspec.Struct, frozen=True, tag="rewrite_about", forbid_unknown_fields=True):
    """Replace the bot-owned `about.md` with a freshly curated doc (AC2)."""

    content: str


class LogEpisode(msgspec.Struct, frozen=True, tag="log_episode", forbid_unknown_fields=True):
    """Append a dated episode note to the curated log. `tags` is optional and closed."""

    content: str
    tags: tuple[str, ...] = ()


class RewriteSummary(msgspec.Struct, frozen=True, tag="rewrite_summary", forbid_unknown_fields=True):
    """Replace the bot-owned running conversation summary `summary.md` (Story 6.2, AD-15).

    The dream cycle proposes it to keep context bounded — a short running summary core writes
    to the curated tree and the 4.4 prompt assembly injects into later turns. A curated-markdown
    op (mirrors `RewriteAbout`), so it routes through `apply_memory_op`."""

    content: str


#: The closed memory-op union — the curated-memory ops core applies via
#: `CuratedMemory.apply_memory_op`. `capture_learning`/`resolve_learning` (AD-6) are SEPARATE
#: ops — they write sqlite, not the markdown tree — so they are NOT in this union.
MemoryOp = Remember | RewriteAbout | LogEpisode | RewriteSummary


class CaptureLearning(msgspec.Struct, frozen=True, tag="capture_learning", forbid_unknown_fields=True):
    """A hot-path self-observation the worker jots for later consolidation (AD-6, Story 6.1).

    Unlike the curated-memory ops (which `core` writes to the markdown tree), this is written
    to the **sqlite `learnings` table** — raw, queryable, deduped by `pattern_key`, with no
    extra LLM call. Core routes it to the history writer, NOT `apply_memory_op`. The 6.2 dream
    cycle is what later classifies these `pending` learnings and promotes the durable ones into
    curated markdown. NOT a `MemoryOp` (markdown); a distinct sqlite op."""

    observation: str
    pattern_key: str | None = None


class AddFace(msgspec.Struct, frozen=True, tag="add_face", forbid_unknown_fields=True):
    """A proposed expression addition (Story 3.4): the worker proposes it on a `Result`
    and core applies it via `apply_add_face` (Story 3.3's atomic, comment-preserving
    `faces.toml` writer — the sole writer, AD-5). Mirrors `add_face`'s args exactly; the
    closed face schema (non-empty name, in-range well-ordered selection tuples,
    duplicate-unless-`replace`) is enforced there, so a malformed proposal is rejected
    without mutating anything. NOT a memory-op — core dispatches it to the face path."""

    name: str
    valence: tuple[float, float]
    arousal: tuple[float, float]
    energy: tuple[float, float]
    token: str = ""
    replace: bool = False


class ResolveLearning(msgspec.Struct, frozen=True, tag="resolve_learning", forbid_unknown_fields=True):
    """Mark a `pending` learning resolved (Story 6.2, AD-15). The dream cycle proposes this to
    transition a learning core baked into the dream prompt by its `id`: `promoted` (its durable
    knowledge was written to markdown via `remember`/`rewrite_about`) or `pruned` (let go). A
    SQLITE op (like `CaptureLearning`) — core applies a SOFT status transition (never a DELETE),
    so a re-recurring pruned learning resets to `pending` (Story 6.1). NOT a `MemoryOp`. An
    unknown/already-resolved `id` is a no-op (core validates by the row state)."""

    id: int
    status: Literal["promoted", "pruned"]


#: The closed set of ALL ops a worker may propose on `Result.proposed_ops` (Story 4.5): the
#: curated-memory ops + the face op (Story 3.4) + the learnings capture (6.1) + the dream's
#: learning-resolution (6.2). Core dispatches each to its single writer — `apply_memory_op`
#: for memory-ops, `apply_add_face` for the face op, `history.capture_learning` /
#: `history.resolve_learning` for the sqlite learnings ops.
ProposedOp = MemoryOp | AddFace | CaptureLearning | ResolveLearning


class Job(msgspec.Struct, frozen=True, tag="job", forbid_unknown_fields=True):
    """A request body. Minimal contract shell — broker/worker stories (1.4/1.5)
    define the real payload. Carries NO credentials: the broker injects creds
    internally (AD-2), so nothing credential-shaped may ever appear here.
    """

    payload: str


class Result(msgspec.Struct, frozen=True, tag="result", forbid_unknown_fields=True):
    """An outcome body, including the error variant — failures surface as a
    Result, never as an exception across the bus (Consistency Conventions).

    The worker emits this to core (Story 4.5): `payload` is the user-facing reply and
    `proposed_ops` is the closed list of ops the worker parsed from its reply — memory-ops
    (4.2) and the face op (3.4) — which core (sole writer, AD-5) validates+applies.
    `proposed_ops` defaults to empty, so a plain reply with no ops is a non-breaking
    decode (AD-13) — no version bump.
    """

    ok: bool
    payload: str = ""
    error: str | None = None
    proposed_ops: list[ProposedOp] = msgspec.field(default_factory=list)


class Completion(msgspec.Struct, frozen=True, tag="completion", forbid_unknown_fields=True):
    """The broker's reply to the worker (Story 4.5): the raw provider text or an error,
    nothing more. The broker stays a pure egress/safety boundary (AD-2) — it does NOT
    parse pet-domain ops; the worker turns this into a `Result` (parsing `proposed_ops`).
    Same ok/payload/error shape as `Result` minus the ops (which are the worker's job).
    """

    ok: bool
    payload: str = ""
    error: str | None = None


class InboundMessage(msgspec.Struct, frozen=True, tag="inbound-message", forbid_unknown_fields=True):
    """An owner message entering core from a chat-transport adapter (AD-13).

    The transport-agnostic inbound half of the message contract: a CLI, Telegram,
    or web adapter all emit this, so core never knows which surface produced it.
    Single-owner for now; a later multi-user adapter adds `chat_id`/`user_id` as an
    OPTIONAL field with a default — a non-breaking wire add (AD-13/AD-6) — so do not
    introduce one before that story needs it.
    """

    text: str


class OutboundMessage(msgspec.Struct, frozen=True, tag="outbound-message", forbid_unknown_fields=True):
    """A pet reply leaving core for a chat-transport adapter to render (AD-13).

    The transport-agnostic outbound half: core emits this without knowing whether
    the adapter prints to a terminal or posts to a bot. Same single-owner shaping
    note as InboundMessage.
    """

    text: str


class StateSnapshot(msgspec.Struct, frozen=True, tag="state-snapshot", forbid_unknown_fields=True):
    """A face/state snapshot core pushes to the display (AD-5). Core is the sole
    writer; the display never reads shared memory — it renders what arrives.

    `seq` is the per-region monotonic sequence: the display applies latest-wins and
    drops any snapshot whose `seq` is not strictly greater than the latest it has
    accepted for that region. `face` is a minimal placeholder expression token for
    the walking skeleton — the real starter emotion set and the mood->face mapping
    are Story 3.3, not here.
    """

    region: Region
    seq: int
    face: str


class Event(msgspec.Struct, frozen=True, tag="event", forbid_unknown_fields=True):
    """A broadcast pet-lifecycle event (AD-11 routing mode 2, Story 7.2). Core publishes
    it with `dst=None` (the reserved broadcast header); the hub delivers it to the
    plugin-host, which fans it out to the plugins that subscribed to this `event` kind
    (the manifest-built registry from Story 7.1). `event` is the closed `EventKind`; a
    richer per-event payload is an additive field added when a consumer needs it."""

    event: EventKind


#: Body type -> the header `kind` it must travel under (single source of truth
#: for the kind<->body agreement enforced in Envelope.__post_init__).
_KIND_FOR_BODY = {
    Job: MsgKind.JOB,
    Result: MsgKind.RESULT,
    Completion: MsgKind.COMPLETION,
    InboundMessage: MsgKind.INBOUND_MSG,
    OutboundMessage: MsgKind.OUTBOUND_MSG,
    StateSnapshot: MsgKind.STATE_SNAPSHOT,
    Event: MsgKind.EVENT,
}


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
    body: Job | Result | Completion | InboundMessage | OutboundMessage | StateSnapshot | Event
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
    MsgKind.COMPLETION: Actor.WORKER,
    MsgKind.INBOUND_MSG: Actor.CORE,
    MsgKind.OUTBOUND_MSG: Actor.CHAT_TRANSPORT,
    MsgKind.STATE_SNAPSHOT: Actor.DISPLAY,
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
    "Region",
    "EventKind",
    "Job",
    "Result",
    "Completion",
    "InboundMessage",
    "OutboundMessage",
    "StateSnapshot",
    "Event",
    "Remember",
    "RewriteAbout",
    "LogEpisode",
    "RewriteSummary",
    "MemoryOp",
    "CaptureLearning",
    "ResolveLearning",
    "AddFace",
    "ProposedOp",
    "Envelope",
    "ROUTING_TABLE",
    "encode",
    "decode",
]
