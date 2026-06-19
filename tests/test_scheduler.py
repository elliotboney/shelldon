"""Story 5.1 — the core named-job scheduler (AD-14/AD-1).

The scheduler generalizes Epic 3's single in-core interval loops into a set of
**named jobs**, each with its own **cadence** (`interval` / `cron`-style daily /
`idle`-triggered) and **cost tier** (`reflex` vs `turn`). This file covers the pure
policy: due-ness is computed from an injected clock + the `last_interaction` signal
(no `asyncio.sleep` anchors — Epic 2 retro #1), each cadence fires when due and not
before, and `tick()` runs reflex jobs in-core while routing turn jobs to the 5.2
dispatch seam (it never forks here). One bad job logs and the scheduler keeps ticking.
"""

from datetime import UTC, datetime, time

import pytest

from shelldon.core.power import BackoffPolicy, PowerState
from shelldon.core.scheduler import CostTier, Daily, Idle, Interval, Job, Scheduler


def _at(hour: int, minute: int = 0, day: int = 17) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=UTC)


async def _noop() -> None:
    pass


# --- Job model: name / cost_tier round-trip (AC1) ---


def test_job_carries_name_cadence_and_cost_tier():
    job = Job("reflex", Interval(10.0), CostTier.REFLEX, _noop)
    assert job.name == "reflex"
    assert job.cost_tier is CostTier.REFLEX
    assert isinstance(job.cadence, Interval)


def test_cost_tier_has_exactly_reflex_and_turn():
    assert {t.value for t in CostTier} == {"reflex", "turn"}


def test_turn_job_carries_cost_weight_and_prompt():
    """Story 5.2: a turn job declares a budget `cost` (default 1; heavier for a dream
    turn) and a `prompt` the dispatch submits through the arbiter."""
    default = Job("ping", Interval(10.0), CostTier.TURN, prompt="hi")
    assert default.cost == 1
    assert default.prompt == "hi"
    heavy = Job("dream", Interval(10.0), CostTier.TURN, cost=3, prompt="reflect")
    assert heavy.cost == 3


def test_reflex_job_requires_a_run_callable():
    """A reflex job with no `run` would TypeError on every due tick — fail fast instead."""
    with pytest.raises(ValueError):
        Job("reflex", Interval(10.0), CostTier.REFLEX, run=None)


def test_job_rejects_nonpositive_cost():
    """cost=0 lets a turn job bypass the budget; cost<0 refunds spend. Reject both."""
    with pytest.raises(ValueError):
        Job("x", Interval(10.0), CostTier.TURN, cost=0, prompt="p")
    with pytest.raises(ValueError):
        Job("x", Interval(10.0), CostTier.TURN, cost=-1, prompt="p")


def test_job_carries_essential_flag_defaulting_non_essential():
    """Story 5.3: a turn job declares whether it survives battery backoff. Default
    non-essential (skipped first on battery), like 5.2's `cost` default."""
    assert Job("ping", Interval(10.0), CostTier.TURN, prompt="hi").essential is False
    assert Job("alert", Interval(10.0), CostTier.TURN, prompt="hi", essential=True).essential is True


def test_job_carries_prompt_builder_and_history_owner_text():
    """Story 5.4: a proactive turn builds its prompt at dispatch from live state, and
    records a synthetic owner-side marker (no real owner message). Both default None
    (static-prompt turns and owner turns are unchanged)."""
    plain = Job("ping", Interval(10.0), CostTier.TURN, prompt="hi")
    assert plain.prompt_builder is None
    assert plain.history_owner_text is None

    def _build():
        return "a live prompt"

    proactive = Job(
        "proactive", Idle(10.0), CostTier.TURN,
        prompt_builder=_build, history_owner_text="(marker)",
    )
    assert proactive.prompt_builder is _build
    assert proactive.history_owner_text == "(marker)"


# --- Story 5.3: cadence stretch via the `scale` factor (battery backoff) ---


def test_interval_cadence_stretches_with_scale():
    """A scale of N multiplies the effective period: a job due at +60s under scale 1.0 is
    NOT due until +180s under scale 3.0."""
    cad = Interval(60.0)
    last_run = _at(12, 0)
    assert cad.is_due(_at(12, 1), last_run, None, scale=3.0) is False  # +60s < 180s -> not due
    assert cad.is_due(_at(12, 2, ), last_run, None, scale=3.0) is False  # +120s < 180s -> not due
    assert cad.is_due(_at(12, 3), last_run, None, scale=3.0) is True   # +180s -> due
    # scale 1.0 is the un-stretched default (regression with the no-scale call).
    assert cad.is_due(_at(12, 1), last_run, None) == cad.is_due(_at(12, 1), last_run, None, scale=1.0)


def test_idle_cadence_stretches_with_scale():
    """The idle threshold stretches too: a 300s idle job under scale 3.0 needs 900s idle."""
    cad = Idle(300.0)
    interacted = _at(12, 0)
    assert cad.is_due(_at(12, 10), None, interacted, scale=3.0) is False  # 600s < 900s -> not due
    assert cad.is_due(_at(12, 15), None, interacted, scale=3.0) is True   # 900s -> due


def test_daily_cadence_ignores_scale():
    """Daily is once-per-day at/after T — a battery stretch does not move the time-of-day
    trigger (a Daily turn job is instead handled by the turn-skip)."""
    cad = Daily(time(3, 0))
    assert cad.is_due(_at(3, 30), None, None, scale=6.0) is True   # past T, first run -> due regardless of scale
    assert cad.is_due(_at(2, 0), None, None, scale=6.0) is False   # before T -> not due


def test_due_set_threads_the_scale_to_every_cadence():
    sched = Scheduler(now=lambda: _at(12, 1))
    sched.register(Job("fast", Interval(60.0), CostTier.REFLEX, _noop))
    last_run = _at(12, 0)
    sched._last_run["fast"] = last_run  # ran a minute ago
    assert {j.name for j in sched.due(_at(12, 1), scale=1.0)} == {"fast"}   # +60s, scale 1 -> due
    assert {j.name for j in sched.due(_at(12, 1), scale=3.0)} == set()      # +60s, scale 3 (need 180s) -> not due


# --- Interval cadence (AC1) ---


def test_interval_is_due_on_first_run_then_only_after_the_period():
    cad = Interval(10.0)
    now = _at(12, 0)
    assert cad.is_due(now, None, None) is True   # never run -> due
    assert cad.is_due(now, now, None) is False   # just ran -> not due


def test_interval_not_due_before_period_due_at_or_after():
    cad = Interval(60.0)
    last_run = _at(12, 0)
    assert cad.is_due(_at(12, 0).replace(second=30), last_run, None) is False  # +30s -> not due
    assert cad.is_due(_at(12, 1), last_run, None) is True   # +60s -> due
    assert cad.is_due(_at(12, 5), last_run, None) is True   # well past -> due


def test_interval_rejects_nonpositive_or_nan_period():
    with pytest.raises(ValueError):
        Interval(0)
    with pytest.raises(ValueError):
        Interval(-1.0)
    with pytest.raises(ValueError):
        Interval(float("nan"))  # NaN slips past a bare `<= 0` and then never fires


# --- Idle cadence (AC1): fires after N seconds since the last owner interaction ---


def test_idle_never_due_without_an_interaction_signal():
    assert Idle(300.0).is_due(_at(12, 0), None, None) is False


def test_idle_not_due_while_recently_interacted():
    cad = Idle(300.0)
    last_interaction = _at(12, 0)
    assert cad.is_due(_at(12, 1), None, last_interaction) is False  # only 60s idle


def test_idle_due_once_per_idle_stretch_not_every_tick():
    cad = Idle(300.0)
    last_interaction = _at(12, 0)
    now = _at(12, 10)  # 600s idle, past the 300s threshold
    assert cad.is_due(now, None, last_interaction) is True  # crosses the threshold -> due

    # After firing, last_run is set to 'now' (> last_interaction): not due again until a
    # NEW interaction resets the idle clock — so a parked owner isn't pinged every tick.
    assert cad.is_due(_at(12, 11), now, last_interaction) is False

    # A fresh interaction re-arms it; after another idle stretch it's due again.
    newer_interaction = _at(12, 12)
    assert cad.is_due(_at(12, 30), now, newer_interaction) is True


def test_idle_rejects_nonpositive_or_nan_period():
    with pytest.raises(ValueError):
        Idle(0)
    with pytest.raises(ValueError):
        Idle(-5.0)
    with pytest.raises(ValueError):
        Idle(float("nan"))


# --- Daily cron-style cadence (AC1): once per calendar day at/after T (UTC) ---


def test_daily_not_due_before_the_trigger_time():
    cad = Daily(time(3, 0))
    assert cad.is_due(_at(2, 0), None, None) is False  # 02:00 < 03:00


def test_daily_due_once_per_day_at_or_after_T():
    cad = Daily(time(3, 0))
    assert cad.is_due(_at(3, 30), None, None) is True  # first run, past T -> due

    last_run = _at(3, 30)  # already fired today
    assert cad.is_due(_at(4, 0), last_run, None) is False  # same day -> not twice
    assert cad.is_due(_at(3, 30, day=18), last_run, None) is True  # next day past T -> due


def test_daily_rejects_a_tz_aware_trigger_time():
    """`at` is compared against a tz-naive now.time(); a tz-aware time would TypeError
    every tick — reject it at construction (fail fast)."""
    with pytest.raises(ValueError):
        Daily(time(3, 0, tzinfo=UTC))


# --- Registration + the due() set from a fixed clock (AC1) ---


def test_register_rejects_duplicate_names():
    sched = Scheduler(now=lambda: _at(12, 0))
    sched.register(Job("a", Interval(10.0), CostTier.REFLEX, _noop))
    with pytest.raises(ValueError):
        sched.register(Job("a", Interval(20.0), CostTier.REFLEX, _noop))


def test_due_set_is_computed_from_a_fixed_clock():
    sched = Scheduler(now=lambda: _at(12, 10))
    sched.register(Job("fast", Interval(60.0), CostTier.REFLEX, _noop))      # never run -> due
    sched.register(Job("nightly", Daily(time(23, 0)), CostTier.TURN, _noop))  # before 23:00 -> not due
    sched.register(Job("greet", Idle(300.0), CostTier.TURN, _noop))           # no interaction -> not due

    due = {j.name for j in sched.due(_at(12, 10), last_interaction=None)}
    assert due == {"fast"}

    # An owner who's been idle 600s makes the idle job due too.
    due2 = {j.name for j in sched.due(_at(12, 10), last_interaction=_at(12, 0))}
    assert due2 == {"fast", "greet"}


# --- tick(): reflex runs in-core, turn routes to the 5.2 dispatch seam ---


async def test_tick_runs_due_reflex_jobs_in_core():
    ran = []
    sched = Scheduler(now=lambda: _at(12, 0))

    async def run():
        ran.append("reflex")

    sched.register(Job("reflex", Interval(10.0), CostTier.REFLEX, run))
    await sched.tick()
    assert ran == ["reflex"]


async def test_tick_routes_turn_jobs_to_dispatch_never_running_them_directly():
    """AD-14: 'the scheduler never forks directly.' A due turn job is handed to the
    arbiter-bound dispatch hook (Story 5.2 fills it), NOT run() in-core here."""
    dispatched = []
    ran_directly = []

    async def turn_run():
        ran_directly.append("BUG: turn job ran in-core")

    async def dispatch(job):
        dispatched.append(job.name)

    sched = Scheduler(now=lambda: _at(12, 0), dispatch_turn=dispatch)
    sched.register(Job("dream", Interval(10.0), CostTier.TURN, turn_run))
    await sched.tick()

    assert dispatched == ["dream"]
    assert ran_directly == []  # the cost-tier gate kept the fork off the cadence


async def test_tick_guards_a_bad_job_and_keeps_the_others_running():
    ran = []
    sched = Scheduler(now=lambda: _at(12, 0))

    async def boom():
        raise RuntimeError("bad tick")

    async def ok():
        ran.append("ok")

    sched.register(Job("boom", Interval(10.0), CostTier.REFLEX, boom))
    sched.register(Job("ok", Interval(10.0), CostTier.REFLEX, ok))
    await sched.tick()  # must not raise
    assert ran == ["ok"]  # the good job still ran despite the bad one


async def test_tick_does_not_run_jobs_that_are_not_due():
    ran = []
    sched = Scheduler(now=lambda: _at(12, 0))

    async def run():
        ran.append("x")

    sched.register(Job("x", Interval(10.0), CostTier.REFLEX, run))
    await sched.tick()           # first tick: due (never run)
    assert ran == ["x"]
    await sched.tick()           # immediately again, same clock: not due
    assert ran == ["x"]


async def test_failing_job_advances_last_run_so_it_does_not_busy_loop():
    """`last_run` is marked BEFORE the run, so a job that raises still waits its period
    before retrying — it is not re-fired every tick. (tick() at a fixed clock: the failed
    job is not due on the immediate next tick.)"""
    calls = {"n": 0}
    sched = Scheduler(now=lambda: _at(12, 0))

    async def boom():
        calls["n"] += 1
        raise RuntimeError("bad tick")

    sched.register(Job("boom", Interval(10.0), CostTier.REFLEX, boom))
    await sched.tick()   # due -> runs, raises (guarded); last_run set to 'now'
    await sched.tick()   # same clock, <period elapsed -> NOT retried
    assert calls["n"] == 1


# --- Story 5.3: battery backoff in tick() — turn-skip per level, reflexes never skipped ---


def _battery(charge):
    return lambda: PowerState(on_battery=True, charge=charge)


async def test_tick_under_eased_skips_non_essential_turn_runs_essential_and_reflex():
    """On battery with ample charge (EASED): a non-essential turn job is skipped, but an
    essential turn job still dispatches and reflex jobs still run."""
    dispatched, reflex_ran = [], []

    async def dispatch(job):
        dispatched.append(job.name)

    async def reflex_run():
        reflex_ran.append("reflex")

    sched = Scheduler(
        now=lambda: _at(12, 0),
        dispatch_turn=dispatch,
        power=_battery(0.80),  # on battery, charge OK -> EASED
        backoff=BackoffPolicy(),
    )
    sched.register(Job("reflex", Interval(10.0), CostTier.REFLEX, reflex_run))
    sched.register(Job("ping", Interval(10.0), CostTier.TURN, prompt="hi"))                  # non-essential
    sched.register(Job("alert", Interval(10.0), CostTier.TURN, prompt="!", essential=True))  # essential
    await sched.tick()

    assert reflex_ran == ["reflex"]      # reflex never skipped by backoff
    assert dispatched == ["alert"]       # essential turn ran; non-essential 'ping' skipped


async def test_tick_under_low_skips_all_turns_including_essential_reflex_still_runs():
    dispatched, reflex_ran = [], []

    async def dispatch(job):
        dispatched.append(job.name)

    async def reflex_run():
        reflex_ran.append("reflex")

    sched = Scheduler(
        now=lambda: _at(12, 0),
        dispatch_turn=dispatch,
        power=_battery(0.05),  # on battery, < 20% -> LOW
        backoff=BackoffPolicy(),
    )
    sched.register(Job("reflex", Interval(10.0), CostTier.REFLEX, reflex_run))
    sched.register(Job("alert", Interval(10.0), CostTier.TURN, prompt="!", essential=True))
    await sched.tick()

    assert reflex_ran == ["reflex"]  # reflex still runs (only stretched, never skipped)
    assert dispatched == []          # LOW skips ALL turns, including the essential one


async def test_tick_when_plugged_in_dispatches_every_turn():
    """LIVELY (the default plugged-in stub): nothing skipped — regression with pre-5.3."""
    dispatched = []

    async def dispatch(job):
        dispatched.append(job.name)

    sched = Scheduler(now=lambda: _at(12, 0), dispatch_turn=dispatch)  # default power stub -> LIVELY
    sched.register(Job("ping", Interval(10.0), CostTier.TURN, prompt="hi"))
    await sched.tick()
    assert dispatched == ["ping"]


async def test_battery_skipped_turn_advances_last_run_no_busy_retry():
    """A backoff-skipped turn still marks last_run, so it is re-proposed next stretched
    cadence — not retried every tick."""
    dispatched = []

    async def dispatch(job):
        dispatched.append(job.name)

    sched = Scheduler(
        now=lambda: _at(12, 0),
        dispatch_turn=dispatch,
        power=_battery(0.80),
        backoff=BackoffPolicy(),
    )
    sched.register(Job("ping", Interval(10.0), CostTier.TURN, prompt="hi"))
    await sched.tick()
    assert dispatched == []
    assert sched.due(_at(12, 0)) == []  # marked run -> not due again at the same clock


async def test_due_turn_job_without_a_dispatch_hook_is_skipped_not_run_in_core():
    """The production seam (Story 5.1): the runtime wires NO dispatch_turn, so a due
    TURN job is marked run and logged+skipped — never executed in-core (AD-14: nothing
    forks here), never raising, and not re-fired on the next same-clock tick."""
    ran = []
    sched = Scheduler(now=lambda: _at(12, 0))  # no dispatch_turn

    async def turn_run():
        ran.append("BUG: turn job ran in-core")

    sched.register(Job("dream", Interval(10.0), CostTier.TURN, turn_run))
    await sched.tick()
    assert ran == []                      # not run in-core, no dispatch hook -> skipped
    assert sched.due(_at(12, 0)) == []    # marked run -> not due again at the same clock
