"""Story 7.3 — the XP/leveling plugin: private state (AC2), XP rules + draw (AC4),
and the end-to-end CAP-7 proof (AC5).

The plugin is a real bus-only consumer: it earns XP from `message-answered`, owns its
private JSON store (atomic write, its own dir — never core's soul), and draws a
status-bar widget through the host's region-scoped emitter.
"""

import asyncio

from shelldon.contracts import Actor, Envelope, Event, EventKind, MsgKind, Region, Result, StateSnapshot
from shelldon.plugins.xp import XpPlugin, XpState, _load_state, _save_state, make_xp_plugin


# --- AC2: private XP state store ------------------------------------------------

def test_fresh_dir_defaults_to_zero_xp_level_one(tmp_path):
    state = _load_state(tmp_path / "xp" / "state.json")
    assert state == XpState(xp=0)
    assert state.level == 1  # level is a derived property, not a stored field (review D2)


def test_state_persists_and_reloads(tmp_path):
    path = tmp_path / "xp" / "state.json"
    _save_state(path, XpState(xp=250))
    reloaded = _load_state(path)
    assert reloaded == XpState(xp=250)
    assert reloaded.level == 3  # 1 + 250 // 100


def test_level_is_never_persisted_only_xp(tmp_path):
    # The JSON holds ONLY xp — a derived level can't drift out of sync (review D2).
    path = tmp_path / "xp" / "state.json"
    _save_state(path, XpState(xp=250))
    assert path.read_text() == '{"xp":250}'


def test_save_is_atomic_no_temp_left_behind(tmp_path):
    path = tmp_path / "xp" / "state.json"
    _save_state(path, XpState(xp=10))
    assert path.exists()
    # No leftover temp files in the dir (the temp is renamed onto the target).
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_corrupt_state_file_falls_back_to_default(tmp_path):
    path = tmp_path / "xp" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    assert _load_state(path) == XpState()


# --- AC4: XP rules + the widget draw --------------------------------------------

class _CapturingEmit:
    """A fake host handle (Story 7.4) that records the plugin's draws."""

    def __init__(self):
        self.draws: list[tuple[Region, str]] = []

    async def draw(self, region, face):
        self.draws.append((region, face))

    async def emit_event(self, kind):  # pragma: no cover - XP never emits
        pass

    def spawn(self, coro):  # pragma: no cover - XP spawns nothing
        coro.close()


def _xp(tmp_path):
    return make_xp_plugin(state_path=tmp_path / "xp" / "state.json")


async def test_on_start_draws_restored_state(tmp_path):
    _save_state(tmp_path / "xp" / "state.json", XpState(xp=120))
    plugin = _xp(tmp_path)
    emit = _CapturingEmit()
    await plugin.on_start(emit)
    assert emit.draws == [(Region.STATUS_BAR, "Lv2 · 120 XP")]


async def test_message_answered_awards_xp_levels_and_redraws(tmp_path):
    plugin = _xp(tmp_path)
    emit = _CapturingEmit()
    await plugin.on_start(emit)  # draws Lv1 · 0 XP
    emit.draws.clear()

    for _ in range(10):  # 10 × +10 = 100 XP -> level 2
        await plugin.on_event(Event(event=EventKind.MESSAGE_ANSWERED))

    assert plugin.state == XpState(xp=100) and plugin.state.level == 2
    assert emit.draws[-1] == (Region.STATUS_BAR, "Lv2 · 100 XP")
    # Persisted (a fresh load sees the new total — "keeps growing" across restarts).
    assert _load_state(tmp_path / "xp" / "state.json") == XpState(xp=100)


async def test_save_failure_still_redraws_the_widget(tmp_path, monkeypatch):
    # Review patch: a persist failure must NOT skip the redraw — the in-memory XP advanced,
    # so show it (it just won't survive a restart until the next good save).
    import shelldon.plugins.xp as xp_mod

    plugin = _xp(tmp_path)
    emit = _CapturingEmit()
    await plugin.on_start(emit)
    emit.draws.clear()

    def _boom(path, state):
        raise OSError("disk full")

    monkeypatch.setattr(xp_mod, "_save_state", _boom)
    await plugin.on_event(Event(event=EventKind.MESSAGE_ANSWERED))
    assert emit.draws == [(Region.STATUS_BAR, "Lv1 · 10 XP")]  # drawn despite the save fail


def test_unreadable_state_path_logs_and_defaults(tmp_path, caplog):
    # Review patch: a real filesystem error (here: a directory where the file should be)
    # is logged, not silently swallowed, and falls back to a default.
    path = tmp_path / "xp" / "state.json"
    path.mkdir(parents=True)  # a directory AT the state path -> OSError on read
    with caplog.at_level("WARNING", logger="shelldon.plugins.xp"):
        assert _load_state(path) == XpState()
    assert any("could not read" in r.message.lower() for r in caplog.records)


async def test_manifest_subscribes_three_kinds_and_claims_status_bar():
    from shelldon.plugins.xp import MANIFEST

    assert set(MANIFEST.subscribes) == {
        EventKind.MESSAGE_ANSWERED, EventKind.TOOL_USED, EventKind.DAY_ALIVE,
    }
    assert MANIFEST.regions == (Region.STATUS_BAR,)


# --- AC5: end-to-end (core turn -> hub -> host -> XP -> display widget) ----------

async def _poll(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _read_status_bar(reader, timeout=1.0):
    """Read display frames until a STATUS_BAR snapshot — core also pushes FACE snapshots
    (the reply face) to the same display connection, so the widget frames are interleaved."""
    from shelldon.core.bus import read_frame

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        remaining = max(0.01, deadline - loop.time())  # each read shares the outer deadline
        env = await asyncio.wait_for(read_frame(reader), timeout=remaining)
        if (
            env is not None
            and isinstance(env.body, StateSnapshot)
            and env.body.region is Region.STATUS_BAR
        ):
            return env.body
    raise AssertionError("no STATUS_BAR snapshot seen")


async def test_end_to_end_message_answered_grows_xp_and_draws_widget(sock_path, tmp_path):
    from shelldon.core.bus import connect
    from shelldon.core.runtime import Core
    from shelldon.plugins.host import run_plugin_host

    class _NoopSpawner:
        async def ready(self):  # pragma: no cover
            pass

    core = Core(sock_path, _NoopSpawner(), memory_root=tmp_path / "memory")
    await core.bus.start()
    d_reader, _d = await connect(sock_path, Actor.DISPLAY)
    plugin = make_xp_plugin(state_path=tmp_path / "xp" / "state.json")
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[plugin]))
    try:
        await _poll(lambda: core.bus._registry.get(Actor.PLUGIN_HOST) is not None)
        # The on_start initial draw (Lv1 · 0 XP).
        first = await _read_status_bar(d_reader)
        assert first.face == "Lv1 · 0 XP"

        # A successful turn -> core emits message-answered -> XP plugin draws the update.
        core.arbiter.submit("hi")
        core._current_prompt = "hi"
        core._current_turn_id = "t1"
        core.fence.open("t1")
        await core._handle_result(
            Envelope(
                id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
                body=Result(ok=True, payload="ok"), turn_id="t1",
            )
        )
        widget = await _read_status_bar(d_reader)
        assert widget.face == "Lv1 · 10 XP"
        assert _load_state(tmp_path / "xp" / "state.json") == XpState(xp=10)
    finally:
        await core.bus.stop()
        # Don't let a teardown cancellation/timeout mask a real assertion above.
        try:
            await asyncio.wait_for(host_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
