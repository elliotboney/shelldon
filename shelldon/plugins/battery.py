"""The PiSugar battery plugin (B.3) — a pure bus-only HARDWARE plugin that reads the
PiSugar2 UPS charge and draws a top-right battery widget.

Like every plugin it imports only `contracts` + `plugins.manifest` (NEVER `core/`,
import-linter enforced), owns no core state, and does its I/O in a host-owned background
loop (`host.spawn`, cancelled on teardown). The PiSugar server speaks a tiny line protocol
on `127.0.0.1:8423` (`get battery` → `battery: 100`, `get battery_power_plugged` →
`battery_power_plugged: true`); we read it with a plain asyncio socket — 0 new deps, no
`nc`/`echo` subprocess. On a box with no PiSugar (a laptop, an unplugged HAT) the connect
fails and the tick is skipped — the widget just stays blank, nothing crashes (AD-8).
"""

import asyncio
import logging

from shelldon.contracts import Region
from shelldon.plugins.manifest import BasePlugin, PluginManifest

log = logging.getLogger("shelldon.plugins.battery")

#: The PiSugar2 power-server line protocol endpoint (local-only).
PISUGAR_HOST = "127.0.0.1"
PISUGAR_PORT = 8423
#: Re-read the charge this often. Battery moves slowly and an E-Ink full refresh is ~2s, so
#: poll lazily — a frequent poll would flash the panel for no new information (B.3 design).
POLL_INTERVAL_S = 60.0
#: Bound on a single connect/read so a wedged/absent server never stalls the loop.
_QUERY_TIMEOUT_S = 3.0

#: Claim the BATTERY widget region + the PiSugar endpoint as a resource (the host rejects a
#: second claimant of either at load — AD-5, single-writer).
MANIFEST = PluginManifest(
    name="battery",
    regions=(Region.BATTERY,),
    resources=("pisugar:8423",),
)


async def _query(field: str) -> str | None:
    """Ask the PiSugar server one field; return the value string (after ``": "``) or None on
    ANY failure (no server / refused / timeout / malformed). Pure I/O, never raises."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(PISUGAR_HOST, PISUGAR_PORT), _QUERY_TIMEOUT_S
        )
    except (OSError, asyncio.TimeoutError) as exc:
        log.debug("battery: PiSugar connect failed (%s)", exc)
        return None
    try:
        writer.write(f"get {field}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), _QUERY_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError) as exc:
        log.debug("battery: PiSugar read failed (%s)", exc)
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    _, _, value = line.decode(errors="replace").strip().partition(": ")  # "battery: 100"
    return value or None


def _parse_pct(raw: str | None) -> int | None:
    """Parse the charge value into a clamped 0–100 int, or None if absent/unparseable. Pure."""
    if raw is None:
        return None
    try:
        return max(0, min(100, round(float(raw))))
    except ValueError:
        log.debug("battery: unparseable charge %r", raw)
        return None


def _format(pct: int, plugged: bool) -> str:
    """The widget text: charge % + a bolt when on external power. Pure."""
    return f"{pct}%{'⚡' if plugged else ''}"


async def read_battery() -> str | None:
    """Read charge % + plugged state and format the widget, or None if the PiSugar can't be
    read this tick (the caller then leaves the widget unchanged)."""
    pct = _parse_pct(await _query("battery"))
    if pct is None:
        return None
    plugged = (await _query("battery_power_plugged")) == "true"
    return _format(pct, plugged)


class BatteryPlugin(BasePlugin):
    """Polls the PiSugar charge on a host-owned loop and draws the top-right widget."""

    async def on_start(self, host) -> None:
        await super().on_start(host)
        host.spawn(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            try:
                text = await read_battery()
                if text is not None and self._host is not None:
                    await self._host.draw(Region.BATTERY, text)
            except asyncio.CancelledError:
                raise  # host teardown — propagate so the task ends cleanly
            except Exception as exc:  # a sensing hiccup must never kill the loop (AD-8)
                log.warning("battery: poll tick failed (%s)", exc)
            await asyncio.sleep(POLL_INTERVAL_S)


def make_battery_plugin() -> BatteryPlugin:
    return BatteryPlugin(MANIFEST)


#: The discovered plugin instance (the host's `plugin_from_module` picks this up). Built at
#: import like the XP plugin, but touches NO network here — the poll loop starts at on_start.
PLUGIN = make_battery_plugin()
