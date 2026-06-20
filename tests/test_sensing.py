"""Story 7.4 — the sensing plugins: PiSugar2 button (AC2) + BLE presence, pair-first
(AC3). The real hardware is gated (#pragma:no cover); these tests drive the plugins'
LOGIC through injected stub sources and assert what reaches the bus.
"""

import asyncio

import pytest

from shelldon.contracts import Actor, EventKind
from shelldon.core.bus import BusServer
from shelldon.plugins.host import run_plugin_host
from shelldon.plugins.manifest import BasePlugin, PluginManifest


class _Recorder(BasePlugin):
    def __init__(self, name, subscribes):
        super().__init__(PluginManifest(name=name, subscribes=subscribes))
        self.got: list[EventKind] = []

    async def on_event(self, event) -> None:
        self.got.append(event.event)


async def _poll(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# --- AC2: the PiSugar2 button plugin --------------------------------------------

class _StubButtonSource:
    """Yields `n` presses, then blocks (models a real button: waits for the next press)."""

    def __init__(self, n):
        self._q: asyncio.Queue = asyncio.Queue()
        for _ in range(n):
            self._q.put_nowait(object())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()


async def test_button_presses_emit_button_pressed_events(sock_path):
    from shelldon.plugins.sensing_button import make_button_plugin

    button = make_button_plugin(source=_StubButtonSource(2))
    sub = _Recorder("sub", (EventKind.BUTTON_PRESSED,))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[button, sub]))
    try:
        await _poll(lambda: sub.got == [EventKind.BUTTON_PRESSED, EventKind.BUTTON_PRESSED])
        # The sense-loop task is host-owned: teardown cancels it cleanly (no leak / no hang).
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
        assert host_task.done() and host_task.exception() is None
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


async def test_button_plugin_with_no_source_idles(sock_path):
    # Q2: no hardware source configured -> the plugin idles (emits nothing), pet unaffected.
    from shelldon.plugins.sensing_button import make_button_plugin

    button = make_button_plugin(source=None)
    sub = _Recorder("sub", (EventKind.BUTTON_PRESSED,))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[button, sub]))
    try:
        await _poll(lambda: srv._registry.get(Actor.PLUGIN_HOST) is not None)
        await asyncio.sleep(0.1)
        assert sub.got == []  # nothing emitted
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


def test_button_manifest_declares_emit_and_claims_resource():
    from shelldon.plugins.sensing_button import MANIFEST

    # Story 7.5: the button now ALSO declares the affect it emits (the fact + the affect).
    assert set(MANIFEST.emits) == {EventKind.BUTTON_PRESSED, EventKind.NUDGE_EXCITED}
    assert MANIFEST.resources == ("pisugar:button",)


async def test_button_press_emits_the_fact_and_the_affect(sock_path):
    # Story 7.5: a press emits BUTTON_PRESSED (fact) AND NUDGE_EXCITED (affect) so the
    # pet's face reacts. A subscriber to both kinds receives both, in fact-then-affect order.
    from shelldon.plugins.sensing_button import make_button_plugin

    button = make_button_plugin(source=_StubButtonSource(1))
    sub = _Recorder("sub", (EventKind.BUTTON_PRESSED, EventKind.NUDGE_EXCITED))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[button, sub]))
    try:
        await _poll(lambda: sub.got == [EventKind.BUTTON_PRESSED, EventKind.NUDGE_EXCITED])
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


# --- AC3: the BLE presence plugin, pair-first -----------------------------------

class _StubPresence:
    """Yields each scan (a set of seen device ids), then blocks (models a real scanner)."""

    def __init__(self, scans):
        self._q: asyncio.Queue = asyncio.Queue()
        for s in scans:
            self._q.put_nowait(set(s))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()


async def _run_ble(sock_path, *, paired, scans):
    """Run the BLE plugin over a stub scan sequence; return the subscriber's received kinds
    + the captured log records."""
    from shelldon.plugins.sensing_ble import make_ble_plugin

    ble = make_ble_plugin(paired_ids=paired, source=_StubPresence(scans))
    sub = _Recorder("sub", (EventKind.PRESENCE_ARRIVED, EventKind.PRESENCE_LEFT))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[ble, sub]))
    try:
        await _poll(lambda: srv._registry.get(Actor.PLUGIN_HOST) is not None)
        await asyncio.sleep(0.1)  # let every queued scan be processed
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
        return sub.got
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)


async def test_paired_device_arriving_then_leaving(sock_path):
    got = await _run_ble(sock_path, paired={"owner-phone"}, scans=[{"owner-phone"}, set()])
    assert got == [EventKind.PRESENCE_ARRIVED, EventKind.PRESENCE_LEFT]


async def test_stable_presence_emits_only_the_arrival_edge(sock_path):
    got = await _run_ble(sock_path, paired={"owner-phone"}, scans=[{"owner-phone"}, {"owner-phone"}])
    assert got == [EventKind.PRESENCE_ARRIVED]  # only the transition, not every scan


async def test_unpaired_device_is_never_tracked_or_logged(sock_path, caplog):
    # The security AC (AD-8): an arbitrary nearby device is NEVER emitted and NEVER logged.
    with caplog.at_level("DEBUG", logger="shelldon.plugins.sensing_ble"):
        got = await _run_ble(sock_path, paired={"owner-phone"}, scans=[{"stranger-xyz"}, {"stranger-xyz"}])
    assert got == []  # no event for the unpaired device
    assert not any("stranger-xyz" in r.getMessage() for r in caplog.records)  # never logged


def test_ble_manifest_declares_emits_and_claims_resource():
    from shelldon.plugins.sensing_ble import MANIFEST

    # Story 7.5: presence now ALSO declares its affects (arrive=positive, leave=negative).
    assert set(MANIFEST.emits) == {
        EventKind.PRESENCE_ARRIVED, EventKind.PRESENCE_LEFT,
        EventKind.NUDGE_POSITIVE, EventKind.NUDGE_NEGATIVE,
    }
    assert MANIFEST.resources == ("ble:adapter",)


async def test_presence_transitions_emit_fact_then_affect(sock_path):
    # Story 7.5: a paired arrival emits PRESENCE_ARRIVED + NUDGE_POSITIVE; a departure emits
    # PRESENCE_LEFT + NUDGE_NEGATIVE — so the face warms on arrival, dims on departure.
    from shelldon.plugins.sensing_ble import make_ble_plugin

    ble = make_ble_plugin(paired_ids={"owner-phone"}, source=_StubPresence([{"owner-phone"}, set()]))
    sub = _Recorder("sub", (
        EventKind.PRESENCE_ARRIVED, EventKind.PRESENCE_LEFT,
        EventKind.NUDGE_POSITIVE, EventKind.NUDGE_NEGATIVE,
    ))
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[ble, sub]))
    try:
        await _poll(lambda: sub.got == [
            EventKind.PRESENCE_ARRIVED, EventKind.NUDGE_POSITIVE,
            EventKind.PRESENCE_LEFT, EventKind.NUDGE_NEGATIVE,
        ])
        await srv.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
    finally:
        if not host_task.done():
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)
