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

from shelldon.contracts import Actor, Envelope, Event, EventKind, MsgKind, Region, StateSnapshot
from shelldon.core.bus import BusServer, connect, read_frame, write_frame
from shelldon.plugins.host import (
    PluginLoadError,
    _fan_out,
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


class _Recorder(BasePlugin):
    """A fake plugin that records the event kinds its on_event receives (Story 7.2)."""

    def __init__(self, name, subscribes):
        super().__init__(PluginManifest(name=name, subscribes=subscribes))
        self.got: list[EventKind] = []

    async def on_event(self, event: Event) -> None:
        self.got.append(event.event)


async def _poll(predicate, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


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

def test_real_plugins_package_discovers_the_shipped_plugins():
    # The shipped, on-by-default plugins: the XP widget (7.3), the two sensing plugins
    # (7.4, which idle with no hardware source), and the PiSugar battery widget (B.3, which
    # idles when no PiSugar server answers). Infra modules expose no MANIFEST.
    import shelldon.plugins as pkg

    found = discover_plugins(pkg)
    assert {p.manifest.name for p in found} == {"xp", "sensing-button", "sensing-ble", "battery"}


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


# --- Story 7.4 AC1: the plugin event-emit seam (plugins -> bus) -------------------

class _Emitter(BasePlugin):
    """Emits a declared event once in on_start (Story 7.4)."""

    def __init__(self, name, emits, kind):
        super().__init__(PluginManifest(name=name, emits=emits))
        self._kind = kind

    async def on_start(self, host) -> None:
        await super().on_start(host)
        await host.emit_event(self._kind)


async def _host_registered_7_4(srv, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if srv._registry.get(Actor.PLUGIN_HOST) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("plugin-host never registered")


async def test_plugin_emits_a_declared_event_to_subscribers(sock_path):
    emitter = _Emitter("btn", (EventKind.BUTTON_PRESSED,), EventKind.BUTTON_PRESSED)
    sub = _Recorder("sub", (EventKind.BUTTON_PRESSED,))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[emitter, sub]))
    try:
        # emitter.on_start -> host.emit_event -> hub broadcast -> back to host -> sub.on_event
        await _poll(lambda: sub.got == [EventKind.BUTTON_PRESSED], timeout=2.0)
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


async def test_plugin_emitting_an_undeclared_kind_is_dropped(sock_path, caplog):
    # The plugin declares NO emits but tries to emit BUTTON_PRESSED — dropped + logged,
    # the subscriber never receives it (a plugin emits only what its manifest declares).
    emitter = _Emitter("btn", (), EventKind.BUTTON_PRESSED)
    sub = _Recorder("sub", (EventKind.BUTTON_PRESSED,))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    with caplog.at_level("WARNING", logger="shelldon.plugins.host"):
        host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[emitter, sub]))
        try:
            await _host_registered_7_4(srv)
            await asyncio.sleep(0.1)  # give any (wrongly) emitted event time to round-trip
            assert sub.got == []
            assert any("did not declare" in r.message.lower() for r in caplog.records)
            await srv.stop()
            await asyncio.wait_for(host_task, timeout=1.0)
        finally:
            if not host_task.done():
                host_task.cancel()
                await asyncio.gather(host_task, return_exceptions=True)


# --- Story 7.3 AC3: the draw seam (host hands each plugin a region-scoped emitter) ---

class _Drawer(BasePlugin):
    """A fake plugin that draws to a region in on_start (Story 7.3, via the 7.4 host handle)."""

    def __init__(self, name, regions, draw_region, text="hello"):
        super().__init__(PluginManifest(name=name, regions=regions))
        self._draw_region = draw_region
        self._text = text

    async def on_start(self, host) -> None:
        await super().on_start(host)  # stores self._host
        await host.draw(self._draw_region, self._text)


async def _wait_registered(srv, actor, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if srv._registry.get(actor) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{actor} never registered")


async def test_plugin_draws_to_its_claimed_region(sock_path):
    drawer = _Drawer("widget", (Region.STATUS_BAR,), Region.STATUS_BAR, text="Lv1")
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    d_reader, _d_writer = await connect(sock_path, Actor.DISPLAY)
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[drawer]))
    try:
        got = await asyncio.wait_for(read_frame(d_reader), timeout=1.0)
        assert got.kind is MsgKind.STATE_SNAPSHOT
        assert got.src is Actor.PLUGIN_HOST
        assert isinstance(got.body, StateSnapshot)
        assert got.body.region is Region.STATUS_BAR
        assert got.body.face == "Lv1"
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


async def test_emit_to_an_unclaimed_region_is_dropped(sock_path, caplog):
    # The plugin claims STATUS_BAR but tries to draw to FACE (unclaimed) — the host's
    # region-scoped guard drops it (single-writer: a plugin draws only what it claimed).
    drawer = _Drawer("widget", (Region.STATUS_BAR,), Region.FACE, text="nope")
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    d_reader, _d_writer = await connect(sock_path, Actor.DISPLAY)
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[drawer]))
    try:
        await _wait_registered(srv, Actor.PLUGIN_HOST)
        with caplog.at_level("WARNING", logger="shelldon.plugins.host"):
            # Nothing should reach the display; a short read times out.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read_frame(d_reader), timeout=0.2)
        assert any("did not claim" in r.message.lower() or "unclaimed" in r.message.lower() for r in caplog.records)
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


def test_plugin_claiming_the_face_region_is_rejected():
    # Core owns the face region (AD-5) — a plugin may not claim it (closes a 7.1 gap).
    with pytest.raises(PluginLoadError) as exc:
        validate_claims([_plugin("greedy", regions=(Region.FACE,))])
    assert "face" in str(exc.value).lower()


# --- AC1: fan-out via the manifest registry (host-side) -------------------------

async def test_fan_out_delivers_only_to_subscribers():
    a = _Recorder("a", (EventKind.MESSAGE_ANSWERED,))
    b = _Recorder("b", (EventKind.DAY_ALIVE,))
    loaded = validate_claims([a, b])
    await _fan_out(Event(event=EventKind.MESSAGE_ANSWERED), loaded)
    assert a.got == [EventKind.MESSAGE_ANSWERED]  # subscriber got it
    assert b.got == []  # non-subscriber did NOT


async def test_fan_out_isolates_a_raising_plugin(caplog):
    # AD-8: a crashed plugin kills only itself — the other subscriber still fires.
    class _Boom(BasePlugin):
        async def on_event(self, event):
            raise RuntimeError("boom in on_event")

    boom = _Boom(PluginManifest(name="boom", subscribes=(EventKind.MESSAGE_ANSWERED,)))
    rec = _Recorder("rec", (EventKind.MESSAGE_ANSWERED,))
    loaded = validate_claims([boom, rec])  # boom first, so it raises before rec runs
    with caplog.at_level("WARNING", logger="shelldon.plugins.host"):
        await _fan_out(Event(event=EventKind.MESSAGE_ANSWERED), loaded)
    assert rec.got == [EventKind.MESSAGE_ANSWERED]  # not aborted by boom's exception
    assert any("boom" in r.message.lower() for r in caplog.records)


# --- AC1/AC3: the host owns the read loop + dispatches received events -----------

async def _host_registered(srv, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if srv._registry.get(Actor.PLUGIN_HOST) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("plugin-host never registered as PLUGIN_HOST")


def _event_env(kind: EventKind) -> Envelope:
    return Envelope(id=kind.value, kind=MsgKind.EVENT, src=Actor.CORE, dst=None, body=Event(event=kind))


async def test_host_dispatches_a_received_event_to_subscribers(sock_path):
    rec = _Recorder("rec", (EventKind.MESSAGE_ANSWERED,))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[rec]))
    try:
        await _host_registered(srv)
        # Write an Event straight to the host's connection (the hub branch is AC3).
        await write_frame(srv._registry[Actor.PLUGIN_HOST], _event_env(EventKind.MESSAGE_ANSWERED))
        await _poll(lambda: rec.got == [EventKind.MESSAGE_ANSWERED])
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
        assert host_task.done() and host_task.exception() is None
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


async def test_host_skips_an_invalid_frame_and_keeps_dispatching(sock_path):
    # Per-frame resilience (transport/display pattern): one bad frame is skipped,
    # the valid Event right after it still reaches the subscriber.
    rec = _Recorder("rec", (EventKind.MESSAGE_ANSWERED,))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[rec]))
    try:
        await _host_registered(srv)
        w = srv._registry[Actor.PLUGIN_HOST]
        # A framed-but-invalid envelope (unsupported schema version -> ValidationError).
        from shelldon.contracts import SCHEMA_VERSION, encode

        bad = encode(
            Envelope(
                id="bad", kind=MsgKind.EVENT, src=Actor.CORE, dst=None,
                body=Event(event=EventKind.DAY_ALIVE), v=SCHEMA_VERSION + 999,
            )
        )
        w.write(len(bad).to_bytes(4, "big") + bad)
        await w.drain()
        await write_frame(w, _event_env(EventKind.MESSAGE_ANSWERED))
        await _poll(lambda: rec.got == [EventKind.MESSAGE_ANSWERED])
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


# --- AC4: the bus-client lifecycle ----------------------------------------------

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
