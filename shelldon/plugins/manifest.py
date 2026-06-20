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

from collections.abc import Coroutine
from typing import Any, Protocol, runtime_checkable

import msgspec

from shelldon.contracts import Event, EventKind, Region


@runtime_checkable
class Host(Protocol):
    """The host capabilities handed to a plugin at `on_start` — the plugin's ONLY door to
    the bus (it never builds an Envelope or touches the connection itself):

    - `draw(region, face)` — push a widget render to a display region the plugin CLAIMED
      (Story 7.3 draw seam; the host validates the claim + manages the per-region seq).
    - `emit_event(kind)` — publish a broadcast event the plugin's manifest DECLARED in
      `emits` (Story 7.4; the host validates + writes the `Event` envelope).
    - `spawn(coro)` — run a background producer loop (e.g. a sensing poll) the host OWNS:
      it is cancelled when the host tears down, so a plugin never leaks a task.
    """

    async def draw(self, region: Region, face: str) -> None: ...

    async def emit_event(self, kind: EventKind) -> None: ...

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None: ...


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
    """The plugin surface the plugin-host drives (Story 7.2). A plugin exposes its
    `manifest` and an `on_event(event)` handler the host calls when a broadcast event
    of a kind the plugin subscribed to arrives.

    The HOST owns the single bus connection and the read loop — a plugin never reads the
    socket itself (Story 7.1 review: N plugins each reading the shared socket corrupts
    framing). A plugin is a pure reactor: it gets the events it subscribed to (via the
    manifest registry) and reacts; emitting events / claiming regions is a later story.

    CONTRACT (Story 7.2 review Decision 2): `on_event` MUST be fast and non-blocking. The
    host fans out sequentially on its single read loop, so a slow/IO-blocking handler
    delays every other subscriber and stalls the loop (and, via backpressure, core's next
    event publish). Offload real work to your own task; do not `await` long operations
    here. A per-plugin timeout is deferred until a real plugin workload exists (7.3).
    """

    manifest: PluginManifest

    async def on_start(self, host: Host) -> None: ...

    async def on_event(self, event: Event) -> None: ...


class BasePlugin:
    """Minimal concrete plugin: holds a manifest, stores the bound host handle, and no-ops
    its lifecycle hooks. Real plugins (the XP widget 7.3, the sensing plugins 7.4) subclass
    this and override `on_start` (draw initial state / start a sense loop) and/or `on_event`
    (react to subscribed events)."""

    def __init__(self, manifest: PluginManifest):
        self.manifest = manifest
        self._host: Host | None = None  # bound by the host via on_start

    async def on_start(self, host: Host) -> None:
        # Host hands the plugin its capabilities once, after connect. Default: store the
        # handle (subclasses override to draw / start a sense loop); react to nothing else.
        self._host = host

    async def on_event(self, event: Event) -> None:
        # Default: react to nothing. Subscribers override this.
        return
