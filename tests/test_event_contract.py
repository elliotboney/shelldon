"""Story 7.2 — the `Event` broadcast wire message (AD-11 routing mode 2).

A closed `Event` body carrying a closed `EventKind`, published with `dst=None`
(the header field AD-11 reserves for broadcast). It round-trips like every other
envelope body, but it is deliberately NOT in `ROUTING_TABLE` — broadcast is a
separate hub path (point-to-point mode 1 owns that table).
"""

import msgspec
import pytest

from shelldon.contracts import (
    ROUTING_TABLE,
    Actor,
    Envelope,
    Event,
    EventKind,
    MsgKind,
    decode,
    encode,
)


def test_event_envelope_round_trips_with_broadcast_dst_none():
    env = Envelope(
        id="evt-1",
        kind=MsgKind.EVENT,
        src=Actor.CORE,
        dst=None,  # AD-11 broadcast reservation
        body=Event(event=EventKind.MESSAGE_ANSWERED),
    )
    back = decode(encode(env))
    assert back.kind is MsgKind.EVENT
    assert back.dst is None
    assert isinstance(back.body, Event)
    assert back.body.event is EventKind.MESSAGE_ANSWERED


def test_event_kind_disagreement_is_rejected():
    # The closed-header guard (Envelope.__post_init__) still binds EVENT to Event.
    with pytest.raises(ValueError):
        Envelope(
            id="bad",
            kind=MsgKind.OUTBOUND_MSG,
            src=Actor.CORE,
            dst=None,
            body=Event(event=EventKind.DAY_ALIVE),
        )


def test_event_is_not_a_point_to_point_routed_kind():
    # Broadcast is mode 2 — EVENT must NOT have a single-destination ROUTING_TABLE row.
    assert MsgKind.EVENT not in ROUTING_TABLE


def test_event_body_rejects_a_typoed_kind_on_decode():
    raw = msgspec.msgpack.encode({"event": "not-a-real-event"})
    with pytest.raises(msgspec.ValidationError):
        msgspec.msgpack.Decoder(Event).decode(raw)
