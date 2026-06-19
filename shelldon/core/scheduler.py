"""core/scheduler — the named-job scheduler (AD-14/AD-1, CAP-2).

Generalizes Epic 3's single in-core interval loops into a set of **named jobs**,
each with its own **cadence** and **cost tier** — "heartbeat is now just one job."
v1's single heartbeat is replaced by a registry the Core wires at composition (a
general plugin-registration API is Epic 7, not here).

- **Cadence** (when a job is due): `Interval` (every N seconds), `Daily` (a minimal
  cron-style "once per calendar day at/after time T", UTC — NOT a full 5-field cron
  grammar, which AD-14 explicitly does not require), and `Idle` (fires once per idle
  stretch after N seconds since the owner's last interaction — the signal already
  lives in `state.last_interaction`). Due-ness is computed from an **injected clock**
  so it is unit-testable without sleeping (the reflex/checkpoint-loop pattern).
- **Cost tier**: `REFLEX` jobs run in-core, no LLM, cheap CPU; `TURN` jobs each cost
  a fork+LLM. AD-14: "the scheduler never forks directly" — so `tick()` runs reflex
  jobs in-core but routes due turn jobs to an injected **dispatch hook** (the arbiter
  gate + cooldown + credit/battery budget Story 5.2 fills). In 5.1 no turn jobs are
  registered; the seam exists and is tested, but nothing forks on a cadence.

LLM-free (AD-1): this imports no provider/worker code — only stdlib datetime.
"""

import enum
import logging
from datetime import datetime, time
from typing import Awaitable, Callable

from shelldon.core.power import BackoffLevel, BackoffPolicy, PowerState

log = logging.getLogger("shelldon.core.scheduler")


class CostTier(enum.Enum):
    """How expensive a job is to run. Gates whether it executes in-core (`REFLEX`)
    or must go through the arbiter's fork+LLM budget (`TURN`, Story 5.2)."""

    REFLEX = "reflex"
    TURN = "turn"


class Cadence:
    """Policy for when a job is due. Subclasses implement `is_due` purely from the
    injected `now`, the job's `last_run` (None until first run), and the owner's
    `last_interaction` (a tz-aware datetime, or None) — no I/O, no sleeps.

    `scale` (Story 5.3) is the battery backoff stretch factor (>= 1.0): an `Interval`/`Idle`
    cadence multiplies its period by it so jobs fire LESS often on battery; `Daily` ignores
    it (it is already once-per-day). Default 1.0 = un-stretched (plugged in / LIVELY)."""

    def is_due(
        self,
        now: datetime,
        last_run: datetime | None,
        last_interaction: datetime | None,
        scale: float = 1.0,
    ) -> bool:
        raise NotImplementedError


class Interval(Cadence):
    """Due when `period_s` seconds have elapsed since the last run (due immediately on
    first run). Covers the reflex tick and the periodic checkpoint flush."""

    def __init__(self, period_s: float) -> None:
        # `not (period_s > 0)` rejects zero/negative AND NaN (NaN > 0 is False) — a NaN
        # period would slip past a bare `<= 0` and then fire only on the first tick.
        if not (period_s > 0):
            raise ValueError(f"interval period must be positive, got {period_s!r}")
        self.period_s = period_s

    def is_due(self, now, last_run, last_interaction, scale=1.0):
        return last_run is None or (now - last_run).total_seconds() >= self.period_s * scale


class Idle(Cadence):
    """Due once after the owner has been idle `period_s` seconds. Fires a single time
    per idle stretch — once it runs, `last_run` advances past `last_interaction`, so it
    won't re-fire until a fresh interaction re-arms the clock (a parked owner is not
    pinged every tick). No usable interaction signal -> never due."""

    def __init__(self, period_s: float) -> None:
        # `not (period_s > 0)` also rejects NaN (see Interval) — a NaN idle period would
        # otherwise never compare True and the idle job would silently never fire.
        if not (period_s > 0):
            raise ValueError(f"idle period must be positive, got {period_s!r}")
        self.period_s = period_s

    def is_due(self, now, last_run, last_interaction, scale=1.0):
        if last_interaction is None:
            return False
        if (now - last_interaction).total_seconds() < self.period_s * scale:
            return False
        return last_run is None or last_run <= last_interaction


class Daily(Cadence):
    """Minimal cron-style cadence: due once per calendar day at/after the trigger time
    `at` (interpreted in `now`'s frame — UTC, since the clock injects `datetime.now(UTC)`).

    Intentionally NOT a full cron grammar — AD-14's named jobs (e.g. a nightly dream
    cycle, Epic 6) need only a daily at-time trigger; a 5-field parser would be
    speculative scope. Limitation: one fixed time-of-day per job, no weekday/month rules.
    """

    def __init__(self, at: time) -> None:
        # `at` is compared against a tz-naive `now.time()` (UTC frame). A tz-aware `at`
        # would raise TypeError on that comparison every tick — reject it at construction
        # (fail fast) instead of silently failing the job forever.
        if at.tzinfo is not None:
            raise ValueError(f"Daily trigger time must be tz-naive (interpreted as UTC), got {at!r}")
        self.at = at

    def is_due(self, now, last_run, last_interaction, scale=1.0):
        # `scale` is ignored: a Daily job is already once-per-day; stretching its
        # time-of-day trigger is meaningless. A Daily TURN job on battery is instead
        # handled by the scheduler's turn-skip (Story 5.3).
        if now.timetz().replace(tzinfo=None) < self.at:
            return False
        return last_run is None or last_run.date() < now.date()


class Job:
    """A registered unit of self-driven life: a `name`, a `cadence` (when), a
    `cost_tier` (how expensive), and the tier-specific payload —

    - `run`: an async callable taking no args (REFLEX jobs — executed in-core);
    - `prompt` + `cost`: a TURN job's prompt and its budget weight (Story 5.2). `cost`
      (default 1) is how many units the job spends against the daily turn budget — a
      heavier turn (a future dream turn) declares a larger `cost`. Ignored for reflex
      jobs (they never hit the budget).
    - `essential` (Story 5.3): whether a TURN job survives battery backoff. A non-essential
      turn (the default) is skipped on battery (EASED); an essential one runs until charge
      is critically low (LOW skips all turns). Ignored for reflex jobs (never skipped).
    - `prompt_builder` + `history_owner_text` (Story 5.4): a proactive turn has no static
      prompt and no owner message. `prompt_builder` (a no-arg callable resolved AT DISPATCH)
      yields the live prompt from current state; `history_owner_text` is the synthetic
      owner-side text recorded for a turn with no real owner utterance. Both default None —
      a static-`prompt` turn and an owner turn are unchanged."""

    def __init__(
        self,
        name: str,
        cadence: Cadence,
        cost_tier: CostTier,
        run: Callable[[], Awaitable[None]] | None = None,
        *,
        cost: int = 1,
        prompt: str | None = None,
        essential: bool = False,
        prompt_builder: Callable[[], str] | None = None,
        history_owner_text: str | None = None,
    ) -> None:
        # A reflex job is run in-core via `run()` every due tick — without a callable it
        # would TypeError on every tick (caught + logged forever). Fail fast instead.
        if cost_tier is CostTier.REFLEX and run is None:
            raise ValueError(f"reflex job {name!r} needs a `run` callable")
        # `cost` is a positive turn-count weight; 0 would let a turn job bypass the budget
        # entirely and a negative would REFUND spend on each admission. Reject both.
        if cost < 1:
            raise ValueError(f"job {name!r} cost must be a positive integer, got {cost!r}")
        self.name = name
        self.cadence = cadence
        self.cost_tier = cost_tier
        self.run = run
        self.cost = cost
        self.prompt = prompt
        self.essential = essential
        self.prompt_builder = prompt_builder
        self.history_owner_text = history_owner_text


class Scheduler:
    """Owns the named jobs and the per-job `last_run` clock. On each `tick` it runs
    every due reflex job in-core (guarded — one bad job logs and the rest keep going)
    and routes every due turn job to the injected dispatch hook (never forking here).

    The clock is injected (`now`) so due-ness is deterministic in tests; `dispatch_turn`
    is the Story 5.2 seam (arbiter-gated). With no hook wired, a due turn job is logged
    and skipped rather than silently dropped.

    `power` + `backoff` are the Story 5.3 battery seam: each tick reads a `PowerState` (a
    NON-BLOCKING cached read — the real PiSugar2 read is a plugin-host plugin pushing
    updates, Epic 7) and the `BackoffPolicy` maps it to a cadence stretch + a turn-skip.
    Both default to the plugged-in stub / default policy, so an un-instrumented scheduler
    behaves exactly as 5.1/5.2 (LIVELY: scale 1.0, nothing skipped)."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        dispatch_turn: Callable[[Job], Awaitable[None]] | None = None,
        power: Callable[[], PowerState] | None = None,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self._now = now
        self._dispatch_turn = dispatch_turn
        self._power = power if power is not None else (lambda: PowerState())
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._jobs: list[Job] = []
        self._last_run: dict[str, datetime] = {}

    def register(self, job: Job) -> None:
        if any(j.name == job.name for j in self._jobs):
            raise ValueError(f"duplicate job name {job.name!r}")
        self._jobs.append(job)

    @property
    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def due(self, now: datetime, last_interaction: datetime | None = None, scale: float = 1.0) -> list[Job]:
        """The jobs due at `now` given the idle signal and the battery stretch `scale`
        (Story 5.3) — pure, no side effects (it does NOT advance `last_run`). Callers tick
        via `tick()`; tests assert the set."""
        return [
            j for j in self._jobs
            if j.cadence.is_due(now, self._last_run.get(j.name), last_interaction, scale)
        ]

    async def tick(self, *, last_interaction: datetime | None = None) -> None:
        """Run one scheduler pass: read power, stretch cadences by the backoff level, mark
        each due job as run, then execute reflex jobs in-core / route turn jobs to dispatch
        (skipping non-essential — or, at LOW, all — turn jobs on battery). Every job runs
        under its own guard so one failure logs and the scheduler keeps ticking."""
        now = self._now()
        # Read power ONCE per tick and derive the backoff level (Story 5.3). The reader is
        # non-blocking (a cached value); the level fixes both the cadence stretch (below, via
        # due()) and the turn-skip. Battery is an OUTER gate over the 5.2 budget gate: a
        # skipped turn never reaches the dispatch hook's cooldown/budget check. Guard the read
        # (a future PiSugar2 plugin reader could raise): it runs BEFORE the per-job loop, so an
        # escaping error would kill the resident scheduler task — default to LIVELY instead.
        try:
            level = self._backoff.level(self._power())
        except Exception as exc:
            log.warning("power read failed (%s); defaulting to LIVELY this tick", exc)
            level = BackoffLevel.LIVELY
        scale = self._backoff.cadence_scale(level)
        for job in self.due(now, last_interaction, scale=scale):
            self._last_run[job.name] = now  # mark before running: a failure/skip waits the period, never busy-loops
            try:
                if job.cost_tier is CostTier.REFLEX:
                    await job.run()  # reflex jobs are stretched but NEVER skipped (cheap, no LLM, carry aliveness)
                elif self._backoff.skips(level, essential=job.essential):
                    log.info("turn job %r skipped: battery backoff (%s)", job.name, level.value)
                else:
                    await self._dispatch(job)
            except Exception as exc:
                log.warning("scheduler job %r failed (%s); ticking on", job.name, exc)

    async def _dispatch(self, job: Job) -> None:
        """Hand a due turn job to the arbiter-bound dispatch hook (Story 5.2). Until it
        is wired, log and skip — AD-14 forbids the scheduler forking directly."""
        if self._dispatch_turn is None:
            log.info("turn job %r due — dispatch is Story 5.2 (arbiter-gated); not forking", job.name)
            return
        await self._dispatch_turn(job)
