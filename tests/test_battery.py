"""B.3 — the PiSugar battery plugin (pure parsing + the asyncio line-protocol read).

The real HAT read is verified live on `gotchi`; what's testable off-Pi: the charge parse /
widget format are pure, and `read_battery` speaks the PiSugar line protocol over a plain
socket — exercised here against a loopback stand-in server (no hardware, no `nc`)."""

import asyncio

from shelldon.plugins import battery
from shelldon.plugins.battery import MANIFEST, _format, _parse_pct, read_battery
from shelldon.contracts import Region


def test_parse_pct_clamps_and_rejects_garbage():
    assert _parse_pct("100") == 100
    assert _parse_pct("4.1") == 4  # PiSugar can report a float
    assert _parse_pct("150") == 100  # clamp high
    assert _parse_pct("-5") == 0  # clamp low
    assert _parse_pct("n/a") is None
    assert _parse_pct(None) is None  # no server this tick


def test_format_shows_bolt_only_when_plugged():
    assert _format(87, plugged=False) == "87%"
    assert _format(100, plugged=True) == "100%⚡"


def test_manifest_claims_the_battery_region_single_writer():
    assert MANIFEST.regions == (Region.BATTERY,)
    assert MANIFEST.resources == ("pisugar:8423",)


async def _fake_pisugar(responses: dict[str, str]):
    """A loopback stand-in for the PiSugar power server: read one `get <field>` line, reply
    `<field>: <value>`. Returns (server, port) — the caller points the plugin at the port."""

    async def handle(reader, writer):
        line = (await reader.readline()).decode().strip()  # "get battery"
        field = line.removeprefix("get ").strip()
        writer.write(f"{field}: {responses.get(field, '')}\n".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, battery.PISUGAR_HOST, 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_read_battery_against_fake_server(monkeypatch):
    server, port = await _fake_pisugar({"battery": "73", "battery_power_plugged": "true"})
    monkeypatch.setattr(battery, "PISUGAR_PORT", port)
    async with server:
        assert await read_battery() == "73%⚡"


async def test_read_battery_returns_none_when_server_absent(monkeypatch):
    # Nothing listening (a laptop / unplugged HAT) → None, so the widget is left unchanged.
    monkeypatch.setattr(battery, "PISUGAR_PORT", 1)  # reserved, refused
    assert await read_battery() is None
