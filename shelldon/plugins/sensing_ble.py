"""The BLE presence sensing plugin (Story 7.4, CAP-3). Emits `PRESENCE_ARRIVED` /
`PRESENCE_LEFT` when a previously-PAIRED device transitions in/out of range.

PAIR-FIRST is the security rule (AD-8): ONLY ids in the configured paired set are ever
tracked — an arbitrary nearby device is **never emitted and never logged**. The filter is
the first thing each scan does, so an unpaired id never reaches any code path that could
record or report it. (Pairing UX is deferred — the paired set is private plugin config.)

"Ship the mechanism + seam, gate the hardware": the real `bleak` scan is a lazily-imported
`#pragma:no cover` adapter that runs only on the Pi (so `bleak` is not a hard dep); the
laptop suite feeds a stub `PresenceSource`. With no source configured the plugin idles.

Bus-only + LLM-free: imports only `shelldon.contracts` + `shelldon.plugins.manifest`.
"""

import logging
from collections.abc import AsyncIterator

from shelldon.contracts import EventKind
from shelldon.plugins.manifest import BasePlugin, Host, PluginManifest

log = logging.getLogger("shelldon.plugins.sensing_ble")

#: Yields one scan per sweep: the set of device ids seen this sweep (paired OR not — the
#: plugin filters to paired ids before anything else, so an unpaired id is never recorded).
PresenceSource = AsyncIterator[set[str]]

MANIFEST = PluginManifest(
    name="sensing-ble",
    # The FACTS (a paired device arrived/left) + the AFFECTS (warm on arrival, dim on
    # departure) — declared emits (Story 7.5). Core maps the affect to a bounded mood patch.
    emits=(
        EventKind.PRESENCE_ARRIVED, EventKind.PRESENCE_LEFT,
        EventKind.NUDGE_POSITIVE, EventKind.NUDGE_NEGATIVE,
    ),
    resources=("ble:adapter",),
)


class SensingBlePlugin(BasePlugin):
    """Tracks which PAIRED devices are present and emits only on the in/out transitions."""

    def __init__(self, manifest: PluginManifest, paired_ids: set[str], source: PresenceSource | None = None):
        super().__init__(manifest)
        self._paired = set(paired_ids)
        self._source = source

    async def on_start(self, host: Host) -> None:
        await super().on_start(host)
        if self._source is None:
            log.info("sensing-ble: no source configured; idling (CAP-3)")
            return
        host.spawn(self._sense_loop(host))

    async def _sense_loop(self, host: Host) -> None:
        present: set[str] = set()  # paired ids currently in range
        async for scan in self._source:
            # PAIR-FIRST: keep ONLY paired ids. An unpaired id is dropped here and never
            # touches `present`, an emit, or a log — there is no promiscuous tracking path.
            seen = {device for device in scan if device in self._paired}
            for _ in seen - present:  # paired device arrived (absent -> present)
                await host.emit_event(EventKind.PRESENCE_ARRIVED)   # fact
                await host.emit_event(EventKind.NUDGE_POSITIVE)     # affect — warm on arrival
            for _ in present - seen:  # paired device left (present -> absent)
                await host.emit_event(EventKind.PRESENCE_LEFT)      # fact
                await host.emit_event(EventKind.NUDGE_NEGATIVE)     # affect — dim on departure
            present = seen


def _bleak_presence_source(paired_ids: set[str]) -> PresenceSource:  # pragma: no cover - real BLE on the Pi
    """The real `bleak` scanner (Linux/BlueZ) — lazily imported so `bleak` is not a hard
    dep. It scans, then yields ONLY the paired ids it saw (pair-first at the source too).
    Wired when the hardware is in hand."""
    raise NotImplementedError("bleak presence source is wired on the Pi (Story 7.4 hardware bring-up)")


def make_ble_plugin(*, paired_ids: set[str] | None = None, source: PresenceSource | None = None) -> SensingBlePlugin:
    """Construct the BLE plugin with its paired-id set + an injectable source (tests pass a
    stub; the Pi passes `_bleak_presence_source(paired_ids)`; `None` source idles)."""
    return SensingBlePlugin(MANIFEST, paired_ids=paired_ids or set(), source=source)


#: The discovered instance — empty paired set + no source, so it idles until configured + the
#: real scanner is wired on the Pi (CAP-3).
PLUGIN = make_ble_plugin()
