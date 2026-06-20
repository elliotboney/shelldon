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

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeAlias, runtime_checkable

import msgspec

from shelldon.contracts import Event, EventKind, Region

#: The draw seam (Story 7.3): a region-scoped sender the host binds to a plugin via
#: `on_start`. `emit(region, face)` pushes a render string to a display region the plugin
#: CLAIMED (the host validates the claim + manages the per-region seq). A plugin draws
#: through this — it never builds an Envelope or touches the bus client itself.
Emit: TypeAlias = Callable[[Region, str], Awaitable[None]]


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

    async def on_start(self, emit: Emit) -> None: ...

    async def on_event(self, event: Event) -> None: ...


class BasePlugin:
    """Minimal concrete plugin: holds a manifest, stores the bound draw seam, and no-ops
    its lifecycle hooks. Real plugins (the XP widget 7.3) subclass this and override
    `on_start` (draw initial state) and/or `on_event` (react to subscribed events)."""

    def __init__(self, manifest: PluginManifest):
        self.manifest = manifest
        self._emit = None  # bound by the host via on_start (Story 7.3 draw seam)

    async def on_start(self, emit: Emit) -> None:
        # Host hands the plugin its region-scoped emitter once, after connect. Default:
        # store it (subclasses override to draw an initial widget); react to nothing else.
        self._emit = emit

    async def on_event(self, event: Event) -> None:
        # Default: react to nothing. Subscribers override this.
        return
