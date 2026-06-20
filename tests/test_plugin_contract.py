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
    # The AD-11 closed event-kind set: core-emitted lifecycle kinds (7.2) + plugin-emitted
    # sensing kinds (7.4). All fan out the same way; the manifest declares which a plugin
    # subscribes to / emits.
    assert EventKind.MESSAGE_ANSWERED == "message-answered"
    assert EventKind.TOOL_USED == "tool-used"
    assert EventKind.DAY_ALIVE == "day-alive"
    assert EventKind.BUTTON_PRESSED == "button-pressed"
    assert EventKind.PRESENCE_ARRIVED == "presence-arrived"
    assert EventKind.PRESENCE_LEFT == "presence-left"
    # Story 7.5: generic *affect* kinds any plugin emits to nudge the pet's mood (core
    # maps them to a bounded patch). Distinct from the *fact* kinds above.
    assert EventKind.NUDGE_POSITIVE == "nudge-positive"
    assert EventKind.NUDGE_NEGATIVE == "nudge-negative"
    assert EventKind.NUDGE_EXCITED == "nudge-excited"
    assert EventKind.NUDGE_CALM == "nudge-calm"
    assert {k.value for k in EventKind} == {
        "message-answered", "tool-used", "day-alive",
        "button-pressed", "presence-arrived", "presence-left",
        "nudge-positive", "nudge-negative", "nudge-excited", "nudge-calm",
    }


def test_status_bar_widget_region_exists():
    # A claimable widget region so a plugin manifest has something to claim (AC2/AC3).
    assert Region.STATUS_BAR == "status-bar"
    # core still owns the face region — the new member is additive, not a replacement.
    assert Region.FACE == "face"


def test_eventkind_stays_distinct_from_msgkind():
    # EventKind (the closed broadcast vocabulary) is a SEPARATE enum from MsgKind (the
    # envelope kinds). Story 7.2 adds MsgKind.EVENT (the broadcast envelope kind) + the
    # Event body, but the EventKind *values* (message-answered, …) are never MsgKind
    # members, and the additive change did NOT bump the schema version (D5).
    assert SCHEMA_VERSION == 1
    msgkind_values = {k.value for k in MsgKind}
    assert all(ek.value not in msgkind_values for ek in EventKind)


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
