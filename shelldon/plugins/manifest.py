"""The generalized plugin contract (AD-8, Story 7.1): the typed manifest a plugin
declares and the bus-client surface the host drives.

One contract covers BOTH hardware and behavioral plugins — there is no second class
(AD-8). A plugin is a bus client speaking only the `Envelope`/bus vocabulary and
**never imports `core/`** (mechanically enforced by the import-linter; it may use the
shared bus client `shelldon.core.bus`, exactly as `transport`/`display` do). It owns
PRIVATE state, may emit/subscribe to closed `EventKind` events (fan-out is Story 7.2),
and may claim display regions / hardware resources — the host rejects conflicting
claims at load (AD-5: no two writers per region/resource).
"""

import logging
from typing import Protocol, runtime_checkable

import msgspec

from shelldon.contracts import EventKind, Region

log = logging.getLogger("shelldon.plugins.manifest")


class PluginManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Everything a plugin touches, declared up front (AD-8). A frozen typed struct —
    mirrors the `contracts/` style — so the closed `EventKind`/`Region` enums make a
    typo a decode/construction error, no hand-rolled validation.

    - `subscribes` / `emits`: the closed broadcast event kinds (Story 7.2 fans them out)
    - `resources`: opaque claim strings (`"gpio:17"`, `"ble:AA:BB:.."`) — the host only
      checks them for conflicts here; the actual hardware access is Story 7.4
    - `regions`: claimed display regions (e.g. `Region.STATUS_BAR`) — single-writer (AD-5)
    """

    name: str
    subscribes: tuple[EventKind, ...] = ()
    emits: tuple[EventKind, ...] = ()
    resources: tuple[str, ...] = ()
    regions: tuple[Region, ...] = ()


@runtime_checkable
class Plugin(Protocol):
    """The bus-client surface the plugin-host drives. A plugin exposes its `manifest`
    and a `run(reader, writer)` coroutine the host calls after connecting it to the bus.

    In Story 7.1 nothing is routed (the event fan-out is 7.2), so the default `run`
    stays alive until the hub closes the connection. A real plugin (XP widget 7.3,
    sensing 7.4) overrides `run` with its own per-frame loop — built on the SAME bus
    client and the SAME per-frame resilience the transport/display adapters use.
    """

    manifest: PluginManifest

    async def run(self, reader, writer) -> None: ...


class BasePlugin:
    """Minimal concrete plugin: holds a manifest and a stay-alive `run` (Story 7.1).

    `run` blocks on the hub until EOF, then returns — so the host's lifecycle (connect,
    drive plugins, tear down when the hub goes away) is exercisable end-to-end before any
    event is wired. Subclasses override `run` once 7.2 gives them events to react to.
    """

    def __init__(self, manifest: PluginManifest):
        self.manifest = manifest

    async def run(self, reader, writer) -> None:
        # No traffic to consume yet (events = Story 7.2). Drain the reader so the
        # coroutine ends cleanly when the hub disconnects (read_frame -> None / EOF),
        # mirroring the display's pure-receiver teardown.
        from shelldon.core.bus import read_frame

        while True:
            try:
                env = await read_frame(reader)
            except msgspec.ValidationError:
                # Decodable framing, invalid message: stream still aligned -> skip it.
                continue
            except ValueError as exc:
                # Framing error (oversized/corrupt length): stream offset lost -> end.
                # Log so the operator gets a signal for why the plugin loop ended.
                log.warning("plugin %r ended on a bus framing error: %s", self.manifest.name, exc)
                return
            if env is None:  # hub gone / clean EOF
                return
