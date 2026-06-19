"""Story 7.1 — the plugin CONTRACT layer: the closed manifest vocabulary in
`contracts/` (AC1) and the typed `PluginManifest`/`Plugin` surface in
`shelldon/plugins/manifest.py`.

These are pure declarations + types — no bus traffic, no event routing (that is
Story 7.2). The point is that the manifest references CLOSED enums (`EventKind`,
`Region`) so a typo is a construction/decode error, and that adding the vocabulary
is wire-additive (no SCHEMA_VERSION bump, no new MsgKind / Envelope body).
"""

import msgspec
import pytest

from shelldon.contracts import (
    SCHEMA_VERSION,
    EventKind,
    MsgKind,
    Region,
)
from shelldon.plugins.manifest import PluginManifest


def test_eventkind_is_the_closed_broadcast_set():
    # The AD-11 closed event-kind set the manifest declares subscriptions against.
    assert EventKind.MESSAGE_ANSWERED == "message-answered"
    assert EventKind.TOOL_USED == "tool-used"
    assert EventKind.DAY_ALIVE == "day-alive"
    assert {k.value for k in EventKind} == {"message-answered", "tool-used", "day-alive"}


def test_status_bar_widget_region_exists():
    # A claimable widget region so a plugin manifest has something to claim (AC2/AC3).
    assert Region.STATUS_BAR == "status-bar"
    # core still owns the face region — the new member is additive, not a replacement.
    assert Region.FACE == "face"


def test_vocabulary_is_wire_additive_not_a_new_message():
    # EventKind is a DECLARATION, not a wire body (Story 7.2 adds the Event message).
    # It must NOT have leaked into the MsgKind set or bumped the schema version.
    assert SCHEMA_VERSION == 1
    assert "EventKind" not in {k.name for k in MsgKind}
    assert not hasattr(MsgKind, "EVENT")


def test_manifest_is_typed_and_defaults_empty():
    m = PluginManifest(name="demo")
    assert m.name == "demo"
    assert m.subscribes == ()
    assert m.emits == ()
    assert m.resources == ()
    assert m.regions == ()


def test_manifest_carries_closed_enum_claims():
    m = PluginManifest(
        name="xp",
        subscribes=(EventKind.MESSAGE_ANSWERED, EventKind.DAY_ALIVE),
        regions=(Region.STATUS_BAR,),
        resources=("gpio:17",),
    )
    assert m.subscribes == (EventKind.MESSAGE_ANSWERED, EventKind.DAY_ALIVE)
    assert m.regions == (Region.STATUS_BAR,)
    assert m.resources == ("gpio:17",)


def test_manifest_rejects_a_typoed_event_kind_on_decode():
    # A bad subscription value is a decode error (the closed enum is the guard) —
    # this is why the manifest is typed, not free strings (D1).
    raw = msgspec.msgpack.encode(
        {"name": "bad", "subscribes": ["not-a-real-event"]}
    )
    with pytest.raises(msgspec.ValidationError):
        msgspec.msgpack.Decoder(PluginManifest).decode(raw)
