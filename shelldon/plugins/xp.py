"""The XP / leveling plugin (Story 7.3) — the first REAL plugin, and the proof that a
behavioral capability ships with ZERO `core/` changes (CAP-7, AD-8).

It is a pure bus-only consumer: it subscribes to the pet's lifecycle events (Story 7.2),
earns XP, owns its PRIVATE state (its own JSON file, never core's soul), and draws a
status-bar widget through the host's region-scoped draw seam (Story 7.3). It imports only
`shelldon.contracts` + `shelldon.plugins.manifest` — never `shelldon.core` (import-linter
enforced). Add or remove this file and `core/` is untouched.

XP rules (defaults, D2): `message-answered` = +10 XP; `level = 1 + xp // 100`; the widget
text is `"Lv{level} · {xp} XP"`. It also subscribes to `tool-used`/`day-alive` for v1
parity (their award rules are wired) though those have no emitter yet (Story 7.2 D3) — the
registry simply never delivers an unemitted kind.
"""

import logging
import os
import tempfile
from pathlib import Path

import msgspec

from shelldon.contracts import EventKind, Region
from shelldon.plugins.manifest import BasePlugin, PluginManifest

log = logging.getLogger("shelldon.plugins.xp")

#: The plugin's PRIVATE store — its own dir under ~/.shelldon, never core's memory/state.
DEFAULT_XP_STATE_PATH = Path.home() / ".shelldon" / "plugins" / "xp" / "state.json"

#: XP awarded per lifecycle event kind (D2). Kinds with no emitter yet (tool-used,
#: day-alive — Story 7.2 D3) are wired for v1 parity; they just never fire today.
_AWARDS: dict[EventKind, int] = {
    EventKind.MESSAGE_ANSWERED: 10,
    EventKind.TOOL_USED: 5,
    EventKind.DAY_ALIVE: 25,
}

_XP_PER_LEVEL = 100

#: The closed manifest: subscribe the three lifecycle kinds, claim the status-bar widget.
MANIFEST = PluginManifest(
    name="xp",
    subscribes=(EventKind.MESSAGE_ANSWERED, EventKind.TOOL_USED, EventKind.DAY_ALIVE),
    regions=(Region.STATUS_BAR,),
)


class XpState(msgspec.Struct):
    """The plugin's private, persisted state. ONLY `xp` is stored — `level` is a derived
    `property` (review Decision 2), never a field, so it can't drift out of sync with `xp`
    in the JSON (a hand-edited or torn write can't persist an inconsistent level)."""

    xp: int = 0

    @property
    def level(self) -> int:
        return _level_for(self.xp)


def _level_for(xp: int) -> int:
    return 1 + xp // _XP_PER_LEVEL


def _widget_text(state: XpState) -> str:
    return f"Lv{state.level} · {state.xp} XP"


def _load_state(path: Path) -> XpState:
    """Load the private state, or a fresh default (xp=0) if absent/corrupt/unreadable — a
    bad file must never crash the plugin (and never the host or the soul, AD-8). A real
    filesystem error (permissions, dir-at-path) is logged, not swallowed silently
    (review patch)."""
    try:
        return msgspec.json.decode(Path(path).read_bytes(), type=XpState)
    except FileNotFoundError:
        return XpState()  # first run — expected, not worth a log
    except (msgspec.DecodeError, msgspec.ValidationError) as exc:
        log.warning("xp: corrupt state file %s (%s); starting fresh", path, exc)
        return XpState()
    except OSError as exc:
        log.warning("xp: could not read state file %s (%s); starting fresh", path, exc)
        return XpState()


def _save_state(path: Path, state: XpState) -> None:
    """Persist atomically: temp in the same dir → flush → fsync → os.replace, so a crash
    mid-write leaves the prior good file (mirrors core/state.py:127-149)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = msgspec.json.encode(state)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class XpPlugin(BasePlugin):
    """Earns XP from lifecycle events, persists it privately, and redraws the widget.

    State is loaded lazily at `on_start` (NOT at construction): the module-level `PLUGIN`
    is built at import, but must not touch the filesystem then — its path resolves to the
    module global `DEFAULT_XP_STATE_PATH` lazily, so a test redirecting that global is
    honoured by the discovered production instance."""

    def __init__(self, manifest: PluginManifest, state_path: Path | None = None):
        super().__init__(manifest)
        self._explicit_path = state_path  # None -> resolve DEFAULT_XP_STATE_PATH lazily
        self.state = XpState()  # placeholder until on_start loads the real file

    @property
    def _state_path(self) -> Path:
        return Path(self._explicit_path) if self._explicit_path is not None else DEFAULT_XP_STATE_PATH

    async def on_start(self, emit) -> None:
        # Bind the draw seam, load private state, then draw it so the widget shows on boot.
        await super().on_start(emit)
        self.state = _load_state(self._state_path)
        await self._draw()

    async def on_event(self, event) -> None:
        award = _AWARDS.get(event.event, 0)
        if award == 0:
            return
        self.state = XpState(xp=self.state.xp + award)
        # Persist is best-effort + a synchronous fsync (review Decision 1: fine for a pet
        # bot — one fsync per owner message; `on_event` is contracted non-blocking/low-freq,
        # 7.2 D2). A save failure must NOT skip the redraw (review patch): the in-memory XP
        # advanced, so show it — it just won't survive a restart until the next good save.
        try:
            _save_state(self._state_path, self.state)
        except OSError as exc:
            log.warning("xp: could not persist state to %s (%s); widget shown anyway", self._state_path, exc)
        await self._draw()

    async def _draw(self) -> None:
        if self._emit is not None:
            await self._emit(Region.STATUS_BAR, _widget_text(self.state))


def make_xp_plugin(*, state_path: Path | None = None) -> XpPlugin:
    """Construct the XP plugin with an injectable private-state path (tests pass a tmp;
    None resolves the module-global DEFAULT_XP_STATE_PATH lazily)."""
    return XpPlugin(MANIFEST, state_path=state_path)


#: The discovered plugin instance (the host's `plugin_from_module` picks this up).
PLUGIN = make_xp_plugin()
