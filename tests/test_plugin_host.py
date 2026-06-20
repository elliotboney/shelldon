"""Story 7.1 — the plugin-host: discovery from `plugins/`, load-time conflict
rejection (AC2/AC3), and the bus-client lifecycle (AC4).

The conflict logic is pure (`validate_claims` over `Plugin` objects); discovery is
the thin `pkgutil.iter_modules` shell over a package. The lifecycle test runs the
real host against a real `BusServer`, mirroring the transport/display isolation tests.
"""

import asyncio
import sys
import textwrap
import types

import pytest

from shelldon.contracts import Actor, EventKind, Region
from shelldon.core.bus import BusServer
from shelldon.plugins.host import (
    PluginLoadError,
    discover_plugins,
    run_plugin_host,
    validate_claims,
)
from shelldon.plugins.manifest import BasePlugin, PluginManifest


def _plugin(name, *, regions=(), resources=(), subscribes=()):
    return BasePlugin(
        PluginManifest(
            name=name, regions=regions, resources=resources, subscribes=subscribes
        )
    )


# --- AC3: conflict rejection at load --------------------------------------------

def test_distinct_claims_load_clean():
    loaded = validate_claims(
        [
            _plugin("a", regions=(Region.STATUS_BAR,), resources=("gpio:17",)),
            _plugin("b", resources=("gpio:27",)),
        ]
    )
    assert loaded.regions == {Region.STATUS_BAR: "a"}
    assert loaded.resources == {"gpio:17": "a", "gpio:27": "b"}
    assert len(loaded.plugins) == 2


def test_two_plugins_claiming_one_region_is_a_load_failure():
    with pytest.raises(PluginLoadError) as exc:
        validate_claims(
            [_plugin("a", regions=(Region.STATUS_BAR,)), _plugin("b", regions=(Region.STATUS_BAR,))]
        )
    msg = str(exc.value)
    assert "status-bar" in msg and "a" in msg and "b" in msg


def test_two_plugins_claiming_one_resource_is_a_load_failure():
    with pytest.raises(PluginLoadError) as exc:
        validate_claims([_plugin("a", resources=("gpio:17",)), _plugin("b", resources=("gpio:17",))])
    assert "gpio:17" in str(exc.value)


def test_duplicate_subscription_is_NOT_a_conflict():
    # Broadcast is 1->N: two plugins subscribing the same kind is the normal case.
    loaded = validate_claims(
        [
            _plugin("a", subscribes=(EventKind.MESSAGE_ANSWERED,)),
            _plugin("b", subscribes=(EventKind.MESSAGE_ANSWERED, EventKind.DAY_ALIVE)),
        ]
    )
    names = {p.manifest.name for p in loaded.subscriptions[EventKind.MESSAGE_ANSWERED]}
    assert names == {"a", "b"}
    assert {p.manifest.name for p in loaded.subscriptions[EventKind.DAY_ALIVE]} == {"b"}


# --- AC2: discovery from the plugins package ------------------------------------

def test_real_plugins_package_discovers_nothing_yet():
    # Production load in 7.1 is empty (XP=7.3, sensing=7.4); the infra modules
    # (host/manifest/__init__) expose no MANIFEST, so they self-exclude.
    import shelldon.plugins as pkg

    assert discover_plugins(pkg) == []


def test_discovers_a_real_plugin_module_from_a_package(tmp_path):
    # A real on-disk package the iter_modules shell walks: a plugin module exposing
    # MANIFEST is discovered; a non-plugin module (no MANIFEST) is skipped.
    pkg_dir = tmp_path / "fake_plugins"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "widget.py").write_text(
        textwrap.dedent(
            """
            from shelldon.contracts import Region
            from shelldon.plugins.manifest import BasePlugin, PluginManifest
            MANIFEST = PluginManifest(name="widget", regions=(Region.STATUS_BAR,))
            PLUGIN = BasePlugin(MANIFEST)
            """
        )
    )
    (pkg_dir / "notaplugin.py").write_text("X = 1\n")

    sys.path.insert(0, str(tmp_path))
    try:
        import fake_plugins  # noqa: PLC0415

        found = discover_plugins(fake_plugins)
        assert [p.manifest.name for p in found] == ["widget"]
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("fake_plugins")]:
            del sys.modules[mod]


def test_module_with_manifest_but_no_plugin_instance_is_wrapped(tmp_path):
    # D1: a module may expose only MANIFEST — the host wraps it in BasePlugin.
    mod = types.ModuleType("manifest_only")
    mod.MANIFEST = PluginManifest(name="bare")
    from shelldon.plugins.host import plugin_from_module

    p = plugin_from_module(mod)
    assert p is not None and p.manifest.name == "bare"
    assert isinstance(p, BasePlugin)


def test_module_without_manifest_is_skipped():
    from shelldon.plugins.host import plugin_from_module

    assert plugin_from_module(types.ModuleType("infra")) is None


def test_module_with_malformed_plugin_is_skipped(caplog):
    # Review patch: a module declaring MANIFEST but a PLUGIN that is not a Plugin
    # (e.g. `PLUGIN = 42`) must be isolated at load, not crash post-connect (AD-8:
    # a bad plugin kills only itself).
    from shelldon.plugins.host import plugin_from_module

    mod = types.ModuleType("malformed")
    mod.MANIFEST = PluginManifest(name="malformed")
    mod.PLUGIN = 42
    with caplog.at_level("WARNING", logger="shelldon.plugins.host"):
        assert plugin_from_module(mod) is None
    assert any("not a valid Plugin" in r.message for r in caplog.records)


def test_discovery_skips_a_module_that_fails_to_import(tmp_path, caplog):
    # Review patch: one broken plugin module must not crash the whole host — it is
    # logged and skipped; the healthy plugin still loads (AD-8 isolation).
    pkg_dir = tmp_path / "halfbroken_plugins"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "good.py").write_text(
        textwrap.dedent(
            """
            from shelldon.plugins.manifest import BasePlugin, PluginManifest
            MANIFEST = PluginManifest(name="good")
            PLUGIN = BasePlugin(MANIFEST)
            """
        )
    )
    (pkg_dir / "broken.py").write_text("raise RuntimeError('boom at import')\n")

    sys.path.insert(0, str(tmp_path))
    try:
        import halfbroken_plugins  # noqa: PLC0415

        with caplog.at_level("WARNING", logger="shelldon.plugins.host"):
            found = discover_plugins(halfbroken_plugins)
        assert [p.manifest.name for p in found] == ["good"]
        assert any("failed to import" in r.message for r in caplog.records)
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("halfbroken_plugins")]:
            del sys.modules[mod]


async def test_baseplugin_logs_on_a_framing_error(caplog):
    # Review patch: BasePlugin.run must not exit SILENTLY on a framing ValueError —
    # the operator needs a signal for why a plugin loop ended.
    reader = asyncio.StreamReader()
    reader.feed_data((9_000_000).to_bytes(4, "big"))  # prefix > MAX_FRAME_BYTES (8 MiB)
    plugin = BasePlugin(PluginManifest(name="noisy"))
    with caplog.at_level("WARNING", logger="shelldon.plugins.manifest"):
        await asyncio.wait_for(plugin.run(reader, None), timeout=1.0)
    assert any("framing" in r.message.lower() for r in caplog.records)


# --- AC4: the bus-client lifecycle ----------------------------------------------

async def _host_registered(srv, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if srv._registry.get(Actor.PLUGIN_HOST) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("plugin-host never registered as PLUGIN_HOST")


async def test_host_connects_and_tears_down_on_hub_disconnect(sock_path):
    import shelldon.plugins as pkg  # empty set in 7.1

    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins_package=pkg))
    try:
        await _host_registered(srv)  # connected as PLUGIN_HOST (addressable for 7.2)
        await srv.stop()  # hub goes away -> host tears down cleanly
        await asyncio.wait_for(host_task, timeout=1.0)
        assert host_task.done() and host_task.exception() is None
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


# --- AC6: wired into the composition root --------------------------------------

def test_plugin_host_is_wired_into_both_launchers():
    import inspect

    from shelldon import app

    # in-process launcher starts the host as a task alongside the other actors
    assert "run_plugin_host" in inspect.getsource(app.launch_in_process)
    # production launcher spawns it as a named child process
    assert callable(app._plugin_host_proc)
    assert "_plugin_host_proc" in inspect.getsource(app.launch_multiprocess)
    assert "shelldon-plugin-host" in inspect.getsource(app.launch_multiprocess)


async def test_host_refuses_to_start_on_a_conflicting_claim(sock_path, tmp_path):
    # A conflict must fail-fast at load — BEFORE the host connects to the bus.
    pkg_dir = tmp_path / "conflict_plugins"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for n in ("one", "two"):
        (pkg_dir / f"{n}.py").write_text(
            textwrap.dedent(
                f"""
                from shelldon.contracts import Region
                from shelldon.plugins.manifest import BasePlugin, PluginManifest
                MANIFEST = PluginManifest(name="{n}", regions=(Region.STATUS_BAR,))
                PLUGIN = BasePlugin(MANIFEST)
                """
            )
        )
    sys.path.insert(0, str(tmp_path))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    try:
        import conflict_plugins  # noqa: PLC0415

        with pytest.raises(PluginLoadError):
            await run_plugin_host(sock_path, plugins_package=conflict_plugins)
        # The host never registered — the failure was at load, not after connecting.
        assert srv._registry.get(Actor.PLUGIN_HOST) is None
    finally:
        await srv.stop()
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("conflict_plugins")]:
            del sys.modules[mod]
