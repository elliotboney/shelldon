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

import asyncio
import importlib
import logging
import pkgutil

from shelldon.contracts import Actor, EventKind, Region
from shelldon.core.bus import connect
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


async def run_plugin_host(socket_path: str, *, plugins_package=None) -> None:
    """Run the plugin-host as a bus client (AD-8). Discovers + validates plugins FIRST
    (a conflicting claim raises before any bus connection), connects as
    `Actor.PLUGIN_HOST`, then drives each loaded plugin's `run(reader, writer)`.

    Mirrors the transport/display lifecycle: when the hub goes away (any plugin loop
    ends), the rest are torn down and the connection closed. With zero plugins (the 7.1
    production set), the host is a healthy idle bus client that ends on hub disconnect.

    SINGLE-READER LIMIT (Story 7.1 — resolved into Story 7.2): the host owns ONE bus
    connection and hands the SAME `reader`/`writer` to every plugin's `run`. That is
    safe only while at most one plugin reads — true in 7.1 (only the idle sentinel runs).
    With ≥2 real plugins each calling `read_frame` on the shared reader, frames would
    interleave and corrupt framing. The fix is NOT a `connect()` per plugin — all would
    register as `Actor.PLUGIN_HOST` and clobber each other in the hub's actor-keyed
    registry. Story 7.2 (broadcast fan-out) makes the HOST own the single read loop and
    dispatch events to subscribed plugins via `loaded.subscriptions` (1→N); a plugin
    never reads the socket itself, so `Plugin.run`'s shape is provisional until then.
    """
    if plugins_package is None:
        import shelldon.plugins as plugins_package  # noqa: PLC0415

    loaded = validate_claims(discover_plugins(plugins_package))  # raises before connect
    log.info("plugin-host loaded %d plugin(s)", len(loaded.plugins))

    reader, writer = await connect(socket_path, Actor.PLUGIN_HOST)
    tasks = [asyncio.create_task(p.run(reader, writer)) for p in loaded.plugins]
    if not tasks:
        # No plugins: stay a healthy idle client until the hub disconnects (so the
        # host's lifecycle matches the others even in the empty 7.1 production set).
        tasks = [asyncio.create_task(BasePlugin(PluginManifest(name="_idle")).run(reader, writer))]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise a genuine loop failure (a cancellation is not in `done`).
        for task in done:
            task.result()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
