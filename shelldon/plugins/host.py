"""The plugin-host (AD-8, Story 7.1): one long-lived bus client that discovers
plugins as modules from the `plugins/` package, validates their claims, rejects
conflicts at load, and drives each plugin's bus loop.

Discovery is the thin `pkgutil.iter_modules` shell; the load-time conflict check
(`validate_claims`) is the policy half — single-writer per region/resource (AD-5).
The subscription registry it builds is the AD-11 "registry built at load from plugin
manifests"; Story 7.2 consumes it to fan broadcast events out. In 7.1 nothing is
routed — the host proves the contract: discover, reject conflicts, connect as a bus
client that never imports core's domain.
"""

import importlib
import logging
import pkgutil
import uuid

import msgspec

from shelldon.contracts import Actor, Envelope, Event, EventKind, MsgKind, Region, StateSnapshot
from shelldon.core.bus import connect, read_frame, write_frame
from shelldon.plugins.manifest import BasePlugin, Plugin, PluginManifest

log = logging.getLogger("shelldon.plugins.host")


class PluginLoadError(Exception):
    """A plugin set that cannot be loaded — today, two plugins claiming one
    region/resource (AD-5: no two writers ever target one region). Fail-fast at load,
    never a silent second writer."""


class LoadedPlugins:
    """The validated load result: the plugin instances plus the claim maps and the
    subscription registry built from their manifests (AD-11).

    `regions`/`resources` map a claim -> the (single) owning plugin name. `subscriptions`
    maps each `EventKind` -> the list of plugins subscribed to it (1->N; Story 7.2 fans out).
    """

    def __init__(
        self,
        plugins: list[Plugin],
        regions: dict[Region, str],
        resources: dict[str, str],
        subscriptions: dict[EventKind, list[Plugin]],
    ):
        self.plugins = plugins
        self.regions = regions
        self.resources = resources
        self.subscriptions = subscriptions


def validate_claims(plugins: list[Plugin]) -> LoadedPlugins:
    """Build the claim maps + subscription registry; raise on a conflicting claim.

    A duplicate region or resource across two plugins is a `PluginLoadError` (single
    writer, AD-5). A duplicate SUBSCRIPTION is fine — broadcast is 1->N.
    """
    regions: dict[Region, str] = {}
    resources: dict[str, str] = {}
    subscriptions: dict[EventKind, list[Plugin]] = {}
    for plugin in plugins:
        m: PluginManifest = plugin.manifest
        for region in m.regions:
            if region is Region.FACE:
                raise PluginLoadError(
                    f"plugin {m.name!r} claims the FACE region, which core owns (AD-5) — "
                    f"plugins may only claim widget regions"
                )
            if region in regions:
                raise PluginLoadError(
                    f"display region {region.value!r} claimed by both "
                    f"{regions[region]!r} and {m.name!r} (no two writers per region — AD-5)"
                )
            regions[region] = m.name
        for resource in m.resources:
            if resource in resources:
                raise PluginLoadError(
                    f"resource {resource!r} claimed by both "
                    f"{resources[resource]!r} and {m.name!r} (no two writers per resource — AD-5)"
                )
            resources[resource] = m.name
        for kind in m.subscribes:
            subscriptions.setdefault(kind, []).append(plugin)
    return LoadedPlugins(list(plugins), regions, resources, subscriptions)


def plugin_from_module(module) -> Plugin | None:
    """Turn a discovered module into a `Plugin`, or `None` if it is not a plugin.

    Convention (D1): a plugin module exposes a `MANIFEST: PluginManifest`. It MAY also
    expose a `PLUGIN` instance with a custom `run` (a real behavioral/hardware plugin,
    Story 7.3/7.4); a manifest-only module is wrapped in a stay-alive `BasePlugin`.
    Infrastructure modules (`host`/`manifest`/`__init__`) expose no `MANIFEST`, so they
    self-exclude — discovery never special-cases them by name.
    """
    manifest = getattr(module, "MANIFEST", None)
    if not isinstance(manifest, PluginManifest):
        return None
    plugin = getattr(module, "PLUGIN", None)
    if plugin is None:
        return BasePlugin(manifest)
    if not isinstance(plugin, Plugin):
        # A module declares MANIFEST but its PLUGIN is not a Plugin (e.g. `PLUGIN = 42`):
        # isolate the bad plugin at load (AD-8 — a bad plugin kills only itself), don't
        # let it crash the host post-connect. Skip + log, like a broken import below.
        log.warning(
            "plugin module %r exposes a PLUGIN that is not a valid Plugin (got %s); skipping",
            getattr(module, "__name__", module),
            type(plugin).__name__,
        )
        return None
    return plugin


def discover_plugins(package) -> list[Plugin]:
    """Discover plugins as submodules of `package` (default: the `shelldon.plugins`
    package). Imports each submodule and collects those exposing a manifest.
    """
    found: list[Plugin] = []
    for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except Exception:
            # One broken plugin module must not crash the host (AD-8: a bad plugin kills
            # only itself, not the soul). Log with the traceback and skip it.
            log.warning("skipping plugin module %r — failed to import", info.name, exc_info=True)
            continue
        plugin = plugin_from_module(module)
        if plugin is not None:
            found.append(plugin)
    return found


async def _safe_on_event(plugin: Plugin, event: Event) -> None:
    """Deliver one event to one plugin, isolating a crash (AD-8: a bad plugin kills only
    itself — the other subscribers and the host survive)."""
    try:
        await plugin.on_event(event)
    except Exception:
        log.warning(
            "plugin %r raised in on_event(%s); isolating it",
            getattr(plugin.manifest, "name", plugin),
            event.event.value,
            exc_info=True,
        )


async def _fan_out(event: Event, loaded: LoadedPlugins) -> None:
    """Fan one broadcast event out to exactly the plugins that subscribed to its kind
    (AD-11), using the manifest-built registry from load. A non-subscriber is never
    called; each subscriber is isolated from the others.

    Sequential by design (review Decision 2): `on_event` is contracted to be fast and
    non-blocking, so a slow handler delaying the rest is a plugin bug, not a host one. A
    per-plugin timeout / concurrent fan-out is deferred until a real workload (7.3)."""
    for plugin in loaded.subscriptions.get(event.event, []):
        await _safe_on_event(plugin, event)


def _make_emitter(plugin: Plugin, writer, seqs: dict[Region, int]):
    """Build a plugin's region-scoped draw seam (Story 7.3). The returned `emit(region,
    face)` pushes a `StateSnapshot` to the display ONLY for a region this plugin claimed
    (runtime single-writer guard, on top of the load-time conflict check + the FACE
    rejection) — an unclaimed region is logged + dropped. The host owns the writer and the
    per-region monotonic `seq`, so the plugin never builds an Envelope or touches the bus."""
    claimed = set(plugin.manifest.regions)

    async def emit(region: Region, face: str) -> None:
        if region not in claimed:
            log.warning(
                "plugin %r did not claim region %s; dropping its draw",
                plugin.manifest.name, region.value,
            )
            return
        # Commit the seq only AFTER a successful write (review patch): a failed write must
        # not skip a seq, or a future strict-monotonic display could drop the next frame.
        next_seq = seqs.get(region, 0) + 1
        await write_frame(
            writer,
            Envelope(
                id=uuid.uuid4().hex,
                kind=MsgKind.STATE_SNAPSHOT,
                src=Actor.PLUGIN_HOST,
                dst=Actor.DISPLAY,
                body=StateSnapshot(region=region, seq=next_seq, face=face),
            ),
        )
        seqs[region] = next_seq

    return emit


async def _safe_on_start(plugin: Plugin, emit) -> None:
    """Bind a plugin its draw seam + let it draw initial state, isolating a crash (AD-8).

    Graceful degradation (review Decision 3): if `on_start` raises, the plugin stays loaded
    and keeps receiving events with whatever state it has — `_load_state` no longer raises
    on a bad/unreadable file (it logs + returns a default), so the realistic failure here is
    a transient draw write, which just means the initial widget is skipped. A plugin that
    needs hard disable-on-failure is a future concern (no real workload needs it yet)."""
    try:
        await plugin.on_start(emit)
    except Exception:
        log.warning(
            "plugin %r raised in on_start; isolating it",
            getattr(plugin.manifest, "name", plugin), exc_info=True,
        )


async def run_plugin_host(socket_path: str, *, plugins_package=None, plugins=None) -> None:
    """Run the plugin-host as a bus client (AD-8, Story 7.2). Discovers + validates
    plugins FIRST (a conflicting claim raises before any bus connection), connects as
    `Actor.PLUGIN_HOST`, then owns the SINGLE bus read loop: each broadcast `Event` is
    read once and fanned out to the plugins that subscribed to its kind
    (`loaded.subscriptions`, the manifest registry from Story 7.1).

    The host — not the plugins — owns the reader (Story 7.1 review: N plugins reading one
    shared socket corrupts framing). Per-frame resilience mirrors transport/display: a bad
    envelope is skipped, a framing error or hub EOF ends the loop cleanly. With zero
    plugins (the production set today) the host is a healthy idle client that reads and
    drops events until the hub disconnects.

    `plugins` (a pre-built list) is an injection seam for tests; production discovers from
    `plugins_package` (default: the `shelldon.plugins` package).
    """
    if plugins is None:
        if plugins_package is None:
            import shelldon.plugins as plugins_package  # noqa: PLC0415
        plugins = discover_plugins(plugins_package)
    loaded = validate_claims(plugins)  # raises before connect
    log.info("plugin-host loaded %d plugin(s)", len(loaded.plugins))

    reader, writer = await connect(socket_path, Actor.PLUGIN_HOST)
    # Bind each plugin its region-scoped draw seam + let it draw initial state (Story 7.3).
    seqs: dict[Region, int] = {}
    for plugin in loaded.plugins:
        await _safe_on_start(plugin, _make_emitter(plugin, writer, seqs))
    try:
        while True:
            try:
                env = await read_frame(reader)
            except msgspec.ValidationError as exc:
                log.warning("plugin-host dropping invalid envelope: %s", exc)
                continue
            except ValueError as exc:
                log.warning("plugin-host hit a framing error, ending: %s", exc)
                return
            if env is None:  # hub gone / clean EOF
                return
            if env.kind is MsgKind.EVENT and isinstance(env.body, Event):
                await _fan_out(env.body, loaded)
            else:
                log.warning("plugin-host ignoring non-event envelope %s (%s)", env.id, env.kind)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
