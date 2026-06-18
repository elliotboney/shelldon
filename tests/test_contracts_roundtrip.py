"""M0 required test (AD-10): every envelope type round-trips encode→decode without loss.

Also guards the closed header (AD-11) and the no-creds-on-the-bus invariant (AD-2/NFR9).
"""

import re

import msgspec
import pytest

from shelldon.contracts import (
    ROUTING_TABLE,
    SCHEMA_VERSION,
    Actor,
    AddFace,
    Completion,
    Envelope,
    InboundMessage,
    Job,
    LogEpisode,
    MsgKind,
    OutboundMessage,
    Region,
    Remember,
    Result,
    StateSnapshot,
    decode,
    encode,
)


def _envelopes():
    """One Envelope per body type, covering the header edge cases."""
    return [
        # Job body, with a turn_id (a turn-bound request)
        Envelope(
            id="env-1",
            kind=MsgKind.JOB,
            src=Actor.CORE,
            dst=Actor.BROKER,
            body=Job(payload="hello"),
            turn_id="turn-1",
        ),
        # Result body, no turn_id, broadcast-style dst=None
        Envelope(
            id="env-2",
            kind=MsgKind.RESULT,
            src=Actor.BROKER,
            dst=None,
            body=Result(ok=True, payload="hi"),
        ),
        # Result error variant (errors travel as Result, never an exception over the bus)
        Envelope(
            id="env-3",
            kind=MsgKind.RESULT,
            src=Actor.WORKER,
            dst=Actor.CORE,
            body=Result(ok=False, error="boom"),
            turn_id="turn-3",
        ),
        # InboundMessage: owner -> core over a chat-transport adapter (AD-13)
        Envelope(
            id="env-4",
            kind=MsgKind.INBOUND_MSG,
            src=Actor.CHAT_TRANSPORT,
            dst=Actor.CORE,
            body=InboundMessage(text="hello pet"),
        ),
        # OutboundMessage: core -> chat-transport adapter (AD-13)
        Envelope(
            id="env-5",
            kind=MsgKind.OUTBOUND_MSG,
            src=Actor.CORE,
            dst=Actor.CHAT_TRANSPORT,
            body=OutboundMessage(text="hi back"),
        ),
        # StateSnapshot: core -> display, face region with a monotonic seq (AD-5)
        Envelope(
            id="env-6",
            kind=MsgKind.STATE_SNAPSHOT,
            src=Actor.CORE,
            dst=Actor.DISPLAY,
            body=StateSnapshot(region=Region.FACE, seq=7, face="neutral"),
        ),
        # Completion: broker -> worker, the raw provider text (Story 4.5)
        Envelope(
            id="env-7",
            kind=MsgKind.COMPLETION,
            src=Actor.BROKER,
            dst=Actor.WORKER,
            body=Completion(ok=True, payload="pong"),
            turn_id="turn-7",
        ),
        # Result carrying proposed_ops: worker -> core (Story 4.5)
        Envelope(
            id="env-8",
            kind=MsgKind.RESULT,
            src=Actor.WORKER,
            dst=Actor.CORE,
            body=Result(
                ok=True,
                payload="noted",
                proposed_ops=[
                    Remember(collection="people", name="Alex", content="friend"),
                    LogEpisode(content="a walk", tags=("outdoor",)),
                ],
            ),
            turn_id="turn-8",
        ),
        # Result carrying a face op: worker -> core (Story 3.4 — add_face on the same wire)
        Envelope(
            id="env-9",
            kind=MsgKind.RESULT,
            src=Actor.WORKER,
            dst=Actor.CORE,
            body=Result(
                ok=True,
                payload="adding it",
                proposed_ops=[
                    AddFace(name="smug", valence=(0.3, 1.0), arousal=(-0.2, 0.2), energy=(0.4, 1.0), token=">:)"),
                ],
            ),
            turn_id="turn-9",
        ),
    ]


@pytest.mark.parametrize("env", _envelopes())
def test_envelope_roundtrip(env):
    """encode→decode yields a value-equal Envelope with the correct concrete body type."""
    decoded = decode(encode(env))
    assert decoded == env
    assert type(decoded.body) is type(env.body)


def test_result_proposed_ops_is_non_breaking_default():
    """AD-13: `proposed_ops` is an additive optional field — a plain Result (no ops)
    decodes with an empty list and the schema version is unchanged."""
    plain = Result(ok=True, payload="just text")
    assert plain.proposed_ops == []
    env = Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=plain, turn_id="t",
    )
    decoded = decode(encode(env))
    assert decoded.body.proposed_ops == []
    assert decoded.v == SCHEMA_VERSION  # additive field → no version bump


def test_proposed_ops_decode_to_concrete_op_types():
    """The closed MemoryOp union round-trips by tag back to its concrete types."""
    env = Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=True, proposed_ops=[Remember(collection="facts", name="n", content="c")]),
        turn_id="t",
    )
    op = decode(encode(env)).body.proposed_ops[0]
    assert type(op) is Remember and op.collection == "facts"


def test_add_face_op_round_trips_in_proposed_ops():
    """Story 3.4: the face op rides the same closed proposed_ops union and decodes back
    to AddFace by tag (no SCHEMA_VERSION bump for the widened union)."""
    env = Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=True, proposed_ops=[
            AddFace(name="proud", valence=(0.4, 1.0), arousal=(0.3, 1.0), energy=(0.5, 1.0)),
        ]),
        turn_id="t",
    )
    decoded = decode(encode(env))
    op = decoded.body.proposed_ops[0]
    assert type(op) is AddFace and op.name == "proud" and op.replace is False
    assert decoded.v == SCHEMA_VERSION


def test_default_schema_version():
    env = Envelope(
        id="env-v",
        kind=MsgKind.JOB,
        src=Actor.CORE,
        dst=Actor.WORKER,
        body=Job(payload="x"),
    )
    assert env.v == SCHEMA_VERSION
    assert decode(encode(env)).v == SCHEMA_VERSION


def test_closed_header_fields():
    """AD-11: the header is exactly id/v/kind/src/dst/turn_id (plus the typed body)."""
    names = {f.name for f in msgspec.structs.fields(Envelope)}
    assert names == {"id", "v", "kind", "src", "dst", "turn_id", "body"}


_CRED_PATTERN = re.compile(
    r"token|key|secret|password|api_?key|authorization|credential", re.IGNORECASE
)


def test_job_carries_no_credentials():
    """AD-2 / NFR9: a Job envelope contains no credential fields — creds never travel on the bus."""
    for field in msgspec.structs.fields(Job):
        assert not _CRED_PATTERN.search(field.name), (
            f"Job field {field.name!r} looks like a credential — creds must not ride the bus"
        )


# --- Review hardening (AD-11 closed header, AD-10 versioning) ---------------


def test_unknown_wire_fields_rejected():
    """AD-11: the header is CLOSED — an envelope carrying an unknown field is rejected,
    not silently dropped, so a typo can't smuggle a field past the contract."""
    raw = msgspec.msgpack.encode(
        {
            "id": "x",
            "kind": "job",
            "src": "core",
            "dst": "broker",
            "body": {"type": "job", "payload": "hi"},
            "v": SCHEMA_VERSION,
            "turn_id": None,
            "rogue_field": "sneaky",
        }
    )
    with pytest.raises(msgspec.ValidationError):
        decode(raw)


def test_every_kind_has_a_route():
    """AD-11 point-to-point: every MsgKind must resolve to a destination — a kind
    added without a routing entry would be unroutable on the bus."""
    for kind in MsgKind:
        assert kind in ROUTING_TABLE, f"MsgKind.{kind.name} has no ROUTING_TABLE entry"
        assert isinstance(ROUTING_TABLE[kind], Actor)


def test_kind_must_match_body():
    """A header `kind` that contradicts the body tag is an invalid envelope —
    rejected at construction so the two can never drift."""
    with pytest.raises(ValueError):
        Envelope(
            id="x",
            kind=MsgKind.RESULT,  # contradicts the Job body
            src=Actor.CORE,
            dst=Actor.BROKER,
            body=Job(payload="hi"),
        )


def test_message_kind_must_match_body():
    """The closed-header kind<->body guard (AD-11) bites for the transport message
    types too: an inbound `kind` with an OutboundMessage body is rejected."""
    with pytest.raises(ValueError):
        Envelope(
            id="x",
            kind=MsgKind.INBOUND_MSG,  # contradicts the OutboundMessage body
            src=Actor.CORE,
            dst=Actor.CHAT_TRANSPORT,
            body=OutboundMessage(text="hi"),
        )


def test_state_snapshot_kind_must_match_body():
    """The closed-header kind<->body guard (AD-11) bites for the display snapshot
    too: a STATE_SNAPSHOT kind with a non-snapshot body is rejected."""
    with pytest.raises(ValueError):
        Envelope(
            id="x",
            kind=MsgKind.STATE_SNAPSHOT,  # contradicts the Result body
            src=Actor.CORE,
            dst=Actor.DISPLAY,
            body=Result(ok=True),
        )


def test_consistent_kind_accepted():
    """The happy path still constructs: kind agrees with body."""
    env = Envelope(
        id="x",
        kind=MsgKind.JOB,
        src=Actor.CORE,
        dst=Actor.BROKER,
        body=Job(payload="hi"),
    )
    assert decode(encode(env)) == env


def test_unsupported_schema_version_rejected():
    """AD-10: `v` is the schema version — an envelope from an unsupported version
    is rejected at decode, not accepted as valid."""
    raw = msgspec.msgpack.encode(
        {
            "id": "x",
            "kind": "job",
            "src": "core",
            "dst": "broker",
            "body": {"type": "job", "payload": "hi"},
            "v": SCHEMA_VERSION + 998,
            "turn_id": None,
        }
    )
    with pytest.raises(msgspec.ValidationError):
        decode(raw)
