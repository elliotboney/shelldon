"""Story 5.3 — battery-aware backoff wired into the runtime (AC1, AC2, AC3 / CAP-10).

Drives the REAL `Core` scheduler with a controllable injected power reader: on battery the
scheduler stretches cadences and skips non-essential turn jobs (the battery gate sits OUTER
to the 5.2 budget gate, so a skipped turn never spawns); plugged in it returns to livelier
cadences and dispatches normally. Deterministic — a mutable power holder + the registered
turn job's spawn is the observable, no `asyncio.sleep` anchors.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from shelldon.core.power import PowerState
from shelldon.core.runtime import Core
from shelldon.core.scheduler import CostTier, Interval, Job


class _RecordingSpawner:
    def __init__(self):
        self.spawns: list[tuple[str, str]] = []

    async def ready(self):  # pragma: no cover - run() is not driven here
        pass

    async def spawn_turn(self, turn_id, prompt):
        self.spawns.append((turn_id, prompt))

    async def reap_current(self):
        pass


async def _teardown(core: Core):
    """Cancel any timeout + reap tasks a dispatched turn created (run() isn't driving them)."""
    core._disarm_timeout()
    tasks = list(core._bg)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _core(sock_path, tmp_path, *, power) -> Core:
    return Core(sock_path, _RecordingSpawner(), checkpoint_path=tmp_path / "state.json", power=power)


def _turn_job(name="ping", essential=False):
    return Job(name, Interval(10.0), CostTier.TURN, prompt="...", essential=essential)


# --- AC1: on battery, a due non-essential turn is skipped (never spawns, never spends) ---


async def test_on_battery_due_non_essential_turn_is_skipped(sock_path, tmp_path):
    core = _core(sock_path, tmp_path, power=lambda: PowerState(on_battery=True, charge=0.80))  # EASED
    core.scheduler.register(_turn_job())
    try:
        await core.scheduler.tick()
        assert core.spawner.spawns == []                  # battery gate skipped it before dispatch
        assert core.state.state.budget.turns_used == 0    # budget never touched (outer gate)
        assert core.arbiter.worker_in_flight is False
    finally:
        await _teardown(core)


# --- AC2: plugged in, the same turn dispatches (livelier — through the 5.2 gate) ---


async def test_plugged_in_due_turn_dispatches_and_spends(sock_path, tmp_path):
    core = _core(sock_path, tmp_path, power=lambda: PowerState(on_battery=False, charge=0.10))  # LIVELY (charging)
    core.scheduler.register(_turn_job())
    try:
        await core.scheduler.tick()
        assert len(core.spawner.spawns) == 1              # dispatched through the budget gate -> spawned
        assert core.state.state.budget.turns_used == 1    # spend recorded (5.2 gate ran)
    finally:
        await _teardown(core)


# --- AC1: at low charge, even an essential turn is skipped (LOW skips all turns) ---


async def test_low_charge_skips_even_an_essential_turn(sock_path, tmp_path):
    core = _core(sock_path, tmp_path, power=lambda: PowerState(on_battery=True, charge=0.05))  # LOW (< 20%)
    core.scheduler.register(_turn_job(name="alert", essential=True))
    try:
        await core.scheduler.tick()
        assert core.spawner.spawns == []
        assert core.state.state.budget.turns_used == 0
    finally:
        await _teardown(core)


# --- AC2: a controllable reader returns to livelier dispatch when plugged back in ---


async def test_returns_to_livelier_dispatch_when_plugged_back_in(sock_path, tmp_path):
    """Flip a mutable power holder from battery → plugged and re-tick: the same job that was
    skipped now dispatches. (A fresh turn job is registered after the flip because the first
    tick advanced last_run.)"""
    holder = {"p": PowerState(on_battery=True, charge=0.80)}  # EASED
    core = _core(sock_path, tmp_path, power=lambda: holder["p"])
    core.scheduler.register(_turn_job(name="ping"))
    try:
        await core.scheduler.tick()
        assert core.spawner.spawns == []  # skipped on battery

        holder["p"] = PowerState(on_battery=False, charge=0.90)  # plugged in / ample
        core.scheduler.register(_turn_job(name="ping2"))         # a fresh due job post-flip
        await core.scheduler.tick()
        assert [p[1] for p in core.spawner.spawns] == ["..."]    # now dispatches (livelier)
    finally:
        await _teardown(core)


# --- AC3 / CAP-10: the cadence stretch is demonstrable through the live wiring ---


async def test_cadence_stretch_is_demonstrable_on_battery(sock_path, tmp_path):
    """A job due at period T under LIVELY is NOT due until T × eased_scale under EASED. Proven
    through the live core's backoff policy + power reader (default eased_scale = 3.0)."""
    holder = {"p": PowerState(on_battery=False, charge=1.0)}  # LIVELY
    core = _core(sock_path, tmp_path, power=lambda: holder["p"])
    core.scheduler.register(Job("beat", Interval(60.0), CostTier.REFLEX, _noop_run))
    ran_at = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    core.scheduler._last_run["beat"] = ran_at

    def live_scale():
        return core._backoff.cadence_scale(core._backoff.level(core._power()))

    plus_60s = datetime(2026, 6, 18, 12, 1, tzinfo=UTC)
    plus_180s = datetime(2026, 6, 18, 12, 3, tzinfo=UTC)

    def beat_due(now, scale):
        return "beat" in {j.name for j in core.scheduler.due(now, scale=scale)}

    assert live_scale() == 1.0  # plugged
    assert beat_due(plus_60s, live_scale()) is True   # +60s -> due (un-stretched)

    holder["p"] = PowerState(on_battery=True, charge=0.80)  # EASED -> scale 3.0
    assert live_scale() == 3.0
    assert beat_due(plus_60s, live_scale()) is False    # +60s < 180s -> NOT due (cadence stretched)
    assert beat_due(plus_180s, live_scale()) is True    # +180s -> due again

    await _teardown(core)


async def _noop_run() -> None:  # pragma: no cover - never invoked (the job is never ticked here)
    pass
