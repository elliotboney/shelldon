"""The PiSugar2 button sensing plugin (Story 7.4, CAP-3). Emits `BUTTON_PRESSED` on the
bus when the physical button is pressed — the rest of the system reacts off the same
event stream (the pet's face reaction is Story 7.5).

"Ship the mechanism + seam, gate the hardware" (the fork uid-drop / E-Ink precedent): the
button is read over PiSugar2's local HTTP/socket API, a component-local install-time dep
that runs only on the Pi. The plugin reads from an injectable `ButtonSource` — the laptop
suite feeds a stub; the Pi feeds the real adapter. With NO source configured the plugin
idles (emits nothing), so it's safe to ship on by default (CAP-3 optionality).

Bus-only + LLM-free: imports only `shelldon.contracts` + `shelldon.plugins.manifest`,
never `shelldon.core` (import-linter enforced).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from shelldon.contracts import EventKind
from shelldon.plugins.manifest import BasePlugin, Host, PluginManifest

log = logging.getLogger("shelldon.plugins.sensing_button")

#: Yields once per physical button press (the value is unused — a press is the signal).
ButtonSource = AsyncIterator[Any]

MANIFEST = PluginManifest(
    name="sensing-button",
    # The FACT (a press happened) + the AFFECT (get excited) — two declared emits (Story 7.5).
    # Core maps the affect to a bounded mood patch so the pet's face reacts; other plugins may
    # count the fact. A press emits both.
    emits=(EventKind.BUTTON_PRESSED, EventKind.NUDGE_EXCITED),
    resources=("pisugar:button",),
)


class SensingButtonPlugin(BasePlugin):
    """Spawns a sense loop (host-owned) that emits `BUTTON_PRESSED` on each press."""

    def __init__(self, manifest: PluginManifest, source: ButtonSource | None = None):
        super().__init__(manifest)
        self._source = source

    async def on_start(self, host: Host) -> None:
        await super().on_start(host)
        if self._source is None:
            log.info("sensing-button: no source configured; idling (CAP-3)")
            return
        host.spawn(self._sense_loop(host))

    async def _sense_loop(self, host: Host) -> None:
        async for _ in self._source:
            await host.emit_event(EventKind.BUTTON_PRESSED)   # fact
            await host.emit_event(EventKind.NUDGE_EXCITED)     # affect (the face reacts, Story 7.5)


def _pisugar_button_source() -> ButtonSource:  # pragma: no cover - real hardware on the Pi
    """The real PiSugar2 button over its local HTTP/socket API — lazily imported so the
    laptop suite never needs the dependency. Wired when the hardware is in hand."""
    raise NotImplementedError("PiSugar2 button source is wired on the Pi (Story 7.4 hardware bring-up)")


def make_button_plugin(*, source: ButtonSource | None = None) -> SensingButtonPlugin:
    """Construct the button plugin with an injectable source (tests pass a stub; the Pi
    passes `_pisugar_button_source()`; `None` idles)."""
    return SensingButtonPlugin(MANIFEST, source=source)


#: The discovered instance — idles until a real source is wired on the Pi (CAP-3).
PLUGIN = make_button_plugin(source=None)
