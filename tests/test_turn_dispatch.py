"""Story 5.2 — the gated turn-job dispatch wired into the runtime (AC1, AC2).

Exercises `Core._dispatch_turn_job` (the seam Story 5.1 left for the scheduler's `turn`
tier): a due turn job is admitted only when the arbiter slot is free AND the cooldown has
elapsed AND the daily budget allows it; otherwise deferred/skipped. Admission reserves the
arbiter, spends the budget (persisted), and starts a turn through the normal lifecycle.
Reflex jobs never touch the gate. The budget survives a restart (the whole point).

These drive the dispatch decision + its side effects (spawn / no spawn, budget patch)
directly — deterministic, no bus round-trip. The full turn machinery has its own e2e tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from shelldon.core.runtime import Core
from shelldon.core.scheduler import CostTier, Idle, Interval, Job


def _today_local() -> str:
    return datetime.now(UTC).astimezone().date().isoformat()


class _RecordingSpawner:
    """Records spawns so a test can assert a turn started (or didn't). ready/reap are
    no-ops; spawn_turn never raises (the happy admit path)."""

    def __init__(self):
        self.spawns: list[tuple[str, str]] = []

    async def ready(self):  # pragma: no cover - run() is not driven here
        pass

    async def spawn_turn(self, turn_id, prompt):
        self.spawns.append((turn_id, prompt))

    async def reap_current(self):
        pass


def _turn_job(name="dream", cost=1, prompt="...self-reflect..."):
    return Job(name, Interval(10.0), CostTier.TURN, cost=cost, prompt=prompt)


async def _teardown(core: Core):
    """Cancel the timeout + reap tasks _start_turn created (run() isn't driving them)."""
    core._disarm_timeout()
    tasks = list(core._bg)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- AC1: admit when slot + cooldown + budget all allow ---


async def test_admits_and_starts_a_turn_when_everything_allows(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    try:
        await core._dispatch_turn_job(_turn_job(prompt="ping"))

        assert len(spawner.spawns) == 1            # a turn was started
        assert spawner.spawns[0][1] == "ping"      # with the job's prompt
        assert core.arbiter.worker_in_flight is True  # arbiter slot reserved (≤1, AD-9)
        assert core.state.state.budget.turns_used == 1  # spend recorded
        assert core.state.state.budget.date == _today_local()
        assert core.state.state.budget.last_turn_at is not None
    finally:
        await _teardown(core)


# --- AC1: defer within the cooldown window ---


async def test_defers_within_the_cooldown(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json", turn_cooldown=1800.0)
    core.state.apply_patch({
        "budget.date": _today_local(), "budget.turns_used": 1,
        "budget.last_turn_at": datetime.now(UTC).isoformat(),  # just now -> inside cooldown
    })
    try:
        await core._dispatch_turn_job(_turn_job())
        assert spawner.spawns == []                       # deferred, no turn
        assert core.arbiter.worker_in_flight is False     # slot untouched
        assert core.state.state.budget.turns_used == 1    # budget unchanged
    finally:
        await _teardown(core)


# --- AC2: skip when the daily budget is exhausted ---


async def test_skips_when_daily_budget_exhausted(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json", daily_turn_budget=12)
    core.state.apply_patch({"budget.date": _today_local(), "budget.turns_used": 12})  # full
    try:
        await core._dispatch_turn_job(_turn_job())
        assert spawner.spawns == []
        assert core.arbiter.worker_in_flight is False
        assert core.state.state.budget.turns_used == 12   # not incremented past the cap
    finally:
        await _teardown(core)


# --- AC1: defer when a turn is already in flight (never coalesced into the owner slot) ---


async def test_defers_when_a_turn_is_in_flight(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    core.arbiter.submit("owner is mid-turn")  # reserve the slot (worker_in_flight=True)
    try:
        await core._dispatch_turn_job(_turn_job())
        assert spawner.spawns == []                    # not started
        assert core.arbiter._pending == []             # NOT folded into the owner catch-up slot
        assert core.state.state.budget.turns_used == 0  # no spend
    finally:
        await _teardown(core)


# --- decision 3: per-job cost weight consumes proportionally ---


async def test_cost_weight_spends_multiple_budget_units(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    try:
        await core._dispatch_turn_job(_turn_job(name="dream", cost=3, prompt="dream"))
        assert len(spawner.spawns) == 1
        assert core.state.state.budget.turns_used == 3  # a heavier turn counts for 3
    finally:
        await _teardown(core)


# --- defensive: a turn job with no prompt is skipped, never wedging the arbiter ---


async def test_promptless_turn_job_is_skipped_not_wedging_the_slot(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    bad = Job("misconfigured", Interval(10.0), CostTier.TURN, prompt=None)
    try:
        await core._dispatch_turn_job(bad)
        assert spawner.spawns == []
        assert core.arbiter.worker_in_flight is False  # the slot is NOT left reserved
        assert core.state.state.budget.turns_used == 0  # no spend
    finally:
        await _teardown(core)


# --- failure branch: a spawn failure still counts the spend AND releases the slot ---


class _FailingSpawner(_RecordingSpawner):
    async def spawn_turn(self, turn_id, prompt):
        raise RuntimeError("os.fork() ENOMEM")


async def test_spawn_failure_counts_the_spend_and_releases_the_slot(sock_path, tmp_path):
    """The budget is recorded BEFORE the spawn (conservative for credit protection), so a
    fork that fails still counts; _start_turn's failure path resets the arbiter, so the
    slot is not wedged (5.0 release-safety holds for scheduler turns too)."""
    core = Core(sock_path, _FailingSpawner(), checkpoint_path=tmp_path / "state.json")
    try:
        await core._dispatch_turn_job(_turn_job(prompt="boom"))
        assert core.state.state.budget.turns_used == 1     # counted despite the failed fork
        assert core.arbiter.worker_in_flight is False       # slot released, not wedged
        assert core.fence.current is None                   # fence closed
    finally:
        await _teardown(core)


# --- AC2: reflex jobs are unaffected by an exhausted budget ---


async def test_reflex_job_runs_with_budget_exhausted(sock_path, tmp_path):
    """The gate lives only in the turn path; a reflex job runs regardless of the budget
    and never spends it."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json", daily_turn_budget=12)
    core.state.apply_patch({"budget.date": _today_local(), "budget.turns_used": 12})  # exhausted

    await core._run_reflex_job()  # must not raise, must not touch the budget
    assert core.state.state.budget.turns_used == 12
    assert spawner.spawns == []  # a reflex job never forks


# --- AC1: the daily cap survives a restart (a crash-loop can't re-grant the budget) ---


# --- Story 5.4: proactive dispatch — built prompt + history marker (AC1, AC3) ---


async def test_dispatch_builds_prompt_and_records_marker_not_directive(sock_path, tmp_path):
    """A proactive job builds its prompt at dispatch (worker gets the directive) and records
    a synthetic owner-side marker, NOT the directive, in history (AC3)."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    recorded: list[tuple] = []
    core.history.record_turn = lambda tid, owner, pet, ts: recorded.append((owner, pet))  # spy

    job = Job(
        "proactive", Interval(10.0), CostTier.TURN,
        prompt_builder=lambda: "DIRECTIVE built from state",
        history_owner_text="(shelldon spoke up on its own)",
    )
    try:
        await core._dispatch_turn_job(job)
        assert spawner.spawns[0][1] == "DIRECTIVE built from state"   # worker runs the directive
        core._record_turn("a pet musing")                            # simulate turn completion record
        assert recorded == [("(shelldon spoke up on its own)", "a pet musing")]  # marker, not directive
    finally:
        await _teardown(core)


async def test_dispatch_builder_that_raises_is_skipped_no_wedge(sock_path, tmp_path):
    """A prompt_builder that raises must skip the turn (no spawn, no spend) and leave the
    slot free — the same fail-soft guarantee as a promptless job."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")

    def _boom():
        raise RuntimeError("state read blew up")

    job = Job("proactive", Interval(10.0), CostTier.TURN, prompt_builder=_boom)
    try:
        await core._dispatch_turn_job(job)
        assert spawner.spawns == []
        assert core.arbiter.worker_in_flight is False
        assert core.state.state.budget.turns_used == 0
    finally:
        await _teardown(core)


# --- Story 5.4: the proactive job is registered + fires with NO owner input (CAP-4) ---


async def test_dispatch_whitespace_only_builder_is_skipped(sock_path, tmp_path):
    """A builder returning whitespace-only text is truthy in Python — but a blank prompt must
    skip (no spawn, no spend), not reach the worker."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    job = Job("proactive", Interval(10.0), CostTier.TURN, prompt_builder=lambda: "   \n  ")
    try:
        await core._dispatch_turn_job(job)
        assert spawner.spawns == []
        assert core.state.state.budget.turns_used == 0
        assert core.arbiter.worker_in_flight is False
    finally:
        await _teardown(core)


async def test_proactive_job_is_registered_in_core(sock_path, tmp_path):
    core = Core(sock_path, _RecordingSpawner(), checkpoint_path=tmp_path / "state.json")
    assert any(j.name == "proactive" for j in core.scheduler.jobs)


async def test_proactive_turn_initiates_with_no_owner_input(sock_path, tmp_path):
    """CAP-4: owner idle past the threshold ⇒ the scheduler initiates a turn with NO
    INBOUND_MSG ever delivered — a spawn happens and budget is spent on pure initiative."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json", proactive_idle_interval=1.0)
    long_ago = datetime.now(UTC) - timedelta(seconds=1000)  # owner idle well past 1s
    try:
        await core.scheduler.tick(last_interaction=long_ago)
        assert len(spawner.spawns) == 1                     # a turn started — with zero owner input
        assert core.state.state.budget.turns_used == 1      # budget spent on the proactive turn
        assert spawner.spawns[0][1].strip() != ""           # the built proactive directive is non-empty
    finally:
        await _teardown(core)


async def test_proactive_turn_not_initiated_within_cooldown(sock_path, tmp_path):
    """AC2: when the cooldown is unsatisfied the proactive trigger does NOT initiate, and a
    reflex job still runs (aliveness carries on)."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json", proactive_idle_interval=1.0)
    core.state.apply_patch({  # a scheduler turn just happened -> inside the 30-min cooldown
        "budget.date": _today_local(), "budget.turns_used": 1,
        "budget.last_turn_at": datetime.now(UTC).isoformat(),
    })
    long_ago = datetime.now(UTC) - timedelta(seconds=1000)
    try:
        await core.scheduler.tick(last_interaction=long_ago)
        assert spawner.spawns == []                       # not initiated (cooldown)
        assert core.state.state.budget.turns_used == 1    # no extra spend
    finally:
        await _teardown(core)


def test_last_interaction_dt_parses_defensively(sock_path, tmp_path):
    """The idle signal feed: a valid ISO stamp parses; None/garbage/tz-naive degrade to None
    (never raised) so the Idle cadence simply doesn't fire — mirrors reflexes._idle_seconds."""
    core = Core(sock_path, _RecordingSpawner(), checkpoint_path=tmp_path / "state.json")
    core.state.apply_patch({"last_interaction": "2026-06-18T12:00:00+00:00"})
    assert core._last_interaction_dt() == datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    core.state._state.last_interaction = None
    assert core._last_interaction_dt() is None
    core.state._state.last_interaction = "not-a-timestamp"
    assert core._last_interaction_dt() is None  # never raises
    # A tz-NAIVE ISO stamp parses fine but can't be subtracted from a tz-aware now downstream
    # (TypeError in Idle.is_due, computed before the per-job guard → silences the whole tick).
    # Reject it here, like reflexes._idle_seconds.
    core.state._state.last_interaction = "2026-06-18T12:00:00"  # no UTC offset
    assert core._last_interaction_dt() is None


# --- Story 6.2: the dream job dispatches only when learnings are pending, at cost 3 ---


def _dream_job(core):
    return next(j for j in core.scheduler.jobs if j.name == "dream")


async def test_dream_job_registered_with_cost_3(sock_path, tmp_path):
    core = Core(sock_path, _RecordingSpawner(), checkpoint_path=tmp_path / "state.json")
    dream = _dream_job(core)
    assert dream.cost == 3 and dream.essential is False


async def test_dream_dispatches_when_learnings_pending_spends_three(sock_path, tmp_path):
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    core.history.capture_learning("owner codes late", "night-owl", datetime.now(UTC))
    try:
        await core._dispatch_turn_job(_dream_job(core))
        assert len(spawner.spawns) == 1                       # the dream turn started
        assert "Pending learnings" in spawner.spawns[0][1]    # the built directive carries them
        assert core.state.state.budget.turns_used == 3        # a dream weighs 3 (cost)
    finally:
        await _teardown(core)


async def test_dream_fires_via_scheduler_tick_on_its_idle_cadence(sock_path, tmp_path):
    """End-to-end through the REAL scheduler tick (not _dispatch_turn_job directly): with the
    dream's Idle cadence due and a learning pending, a tick initiates the dream — proving the
    job registration + cadence + dispatch wire. Proactive interval pushed out so only the dream
    is due."""
    spawner = _RecordingSpawner()
    core = Core(
        sock_path, spawner, checkpoint_path=tmp_path / "state.json",
        dream_idle_interval=1.0, proactive_idle_interval=1e9,  # only the dream is due
    )
    core.history.capture_learning("owner codes late", "night-owl", datetime.now(UTC))
    long_ago = datetime.now(UTC) - timedelta(seconds=1000)
    try:
        await core.scheduler.tick(last_interaction=long_ago)
        assert len(spawner.spawns) == 1
        assert "Pending learnings" in spawner.spawns[0][1]   # the dream turn, built from pending
        assert core.state.state.budget.turns_used == 3       # dream cost
    finally:
        await _teardown(core)


def test_dream_prompt_flattens_newlines_in_observations(sock_path, tmp_path):
    """A multi-line observation must stay ONE baked line so the id<->text association in the
    directive isn't scrambled (Epic 5 input-edge sub-list)."""
    core = Core(sock_path, _RecordingSpawner(), checkpoint_path=tmp_path / "state.json")
    core.history.capture_learning("line one\nline two\n  line three", "multi", datetime.now(UTC))
    directive = core._build_dream_prompt()
    learning_line = next(ln for ln in directive.splitlines() if ln.startswith("- [id="))
    assert "\n" not in learning_line and "line one line two line three" in learning_line
    core.history.close()


async def test_dream_skipped_when_no_pending_learnings(sock_path, tmp_path):
    """No pending learnings → _build_dream_prompt returns "" → the empty-prompt skip fires
    (no spawn, no spend) — the dream only costs budget when there's something to consolidate."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    try:
        await core._dispatch_turn_job(_dream_job(core))
        assert spawner.spawns == []
        assert core.state.state.budget.turns_used == 0
    finally:
        await _teardown(core)


async def test_budget_survives_a_restart(sock_path, tmp_path):
    target = tmp_path / "state.json"
    spawner1 = _RecordingSpawner()
    core1 = Core(sock_path, spawner1, checkpoint_path=target, daily_turn_budget=1)
    try:
        await core1._dispatch_turn_job(_turn_job(prompt="first"))
        assert core1.state.state.budget.turns_used == 1  # the single daily turn is spent
        core1.state.checkpoint(target)                   # persist the ledger
    finally:
        await _teardown(core1)

    spawner2 = _RecordingSpawner()
    core2 = Core(sock_path, spawner2, checkpoint_path=target, daily_turn_budget=1)  # "restart"
    try:
        await core2._dispatch_turn_job(_turn_job(prompt="second"))
        assert spawner2.spawns == []  # cap was loaded from disk — the restart did NOT reset it
    finally:
        await _teardown(core2)


# --- Story 7.0 review hardening: the admit section must not leak the arbiter slot ---


async def test_slot_released_when_recording_the_spend_fails(sock_path, tmp_path):
    """Review #1: the arbiter slot is reserved by submit() BEFORE the spend is recorded. If
    apply_patch raises (e.g. a disk error), nothing downstream releases the slot — _start_turn
    is never reached — so it would leak forever and coalesce every later turn into a wedged
    slot. The dispatch must reset the slot before propagating the error."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")

    def _boom(_patch):
        raise RuntimeError("disk full")

    core.state.apply_patch = _boom  # break the spend write (same instance the dispatcher holds)
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            await core._dispatch_turn_job(_turn_job(prompt="ping"))
        assert core.arbiter.worker_in_flight is False  # slot RELEASED, not wedged
        assert spawner.spawns == []                     # the turn never started
    finally:
        await _teardown(core)


async def test_non_str_builder_return_skips_not_crashes(sock_path, tmp_path):
    """Review #2: a prompt_builder that returns a non-str truthy value (a misconfigured
    builder yielding a list/int) must SKIP the cadence — caught and logged — not raise an
    unguarded AttributeError from .strip() that propagates and wedges the dispatch."""
    spawner = _RecordingSpawner()
    core = Core(sock_path, spawner, checkpoint_path=tmp_path / "state.json")
    bad = Job("dream", Interval(10.0), CostTier.TURN, prompt_builder=lambda: [1, 2])
    try:
        await core._dispatch_turn_job(bad)  # must NOT raise
        assert spawner.spawns == []                     # skipped — no turn
        assert core.arbiter.worker_in_flight is False   # slot untouched (never reserved)
        assert core.state.state.budget.turns_used == 0  # no spend
    finally:
        await _teardown(core)
