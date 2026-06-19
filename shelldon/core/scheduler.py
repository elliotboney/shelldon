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

log = logging.getLogger("shelldon.core.scheduler")


class CostTier(enum.Enum):
    """How expensive a job is to run. Gates whether it executes in-core (`REFLEX`)
    or must go through the arbiter's fork+LLM budget (`TURN`, Story 5.2)."""

    REFLEX = "reflex"
    TURN = "turn"


class Cadence:
    """Policy for when a job is due. Subclasses implement `is_due` purely from the
    injected `now`, the job's `last_run` (None until first run), and the owner's
    `last_interaction` (a tz-aware datetime, or None) — no I/O, no sleeps."""

    def is_due(self, now: datetime, last_run: datetime | None, last_interaction: datetime | None) -> bool:
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

    def is_due(self, now, last_run, last_interaction):
        return last_run is None or (now - last_run).total_seconds() >= self.period_s


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

    def is_due(self, now, last_run, last_interaction):
        if last_interaction is None:
            return False
        if (now - last_interaction).total_seconds() < self.period_s:
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

    def is_due(self, now, last_run, last_interaction):
        if now.timetz().replace(tzinfo=None) < self.at:
            return False
        return last_run is None or last_run.date() < now.date()


class Job:
    """A registered unit of self-driven life: a `name`, a `cadence` (when), a
    `cost_tier` (how expensive), and `run` — an async callable taking no args."""

    def __init__(self, name: str, cadence: Cadence, cost_tier: CostTier, run: Callable[[], Awaitable[None]]) -> None:
        self.name = name
        self.cadence = cadence
        self.cost_tier = cost_tier
        self.run = run


class Scheduler:
    """Owns the named jobs and the per-job `last_run` clock. On each `tick` it runs
    every due reflex job in-core (guarded — one bad job logs and the rest keep going)
    and routes every due turn job to the injected dispatch hook (never forking here).

    The clock is injected (`now`) so due-ness is deterministic in tests; `dispatch_turn`
    is the Story 5.2 seam (arbiter-gated). With no hook wired, a due turn job is logged
    and skipped rather than silently dropped."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        dispatch_turn: Callable[[Job], Awaitable[None]] | None = None,
    ) -> None:
        self._now = now
        self._dispatch_turn = dispatch_turn
        self._jobs: list[Job] = []
        self._last_run: dict[str, datetime] = {}

    def register(self, job: Job) -> None:
        if any(j.name == job.name for j in self._jobs):
            raise ValueError(f"duplicate job name {job.name!r}")
        self._jobs.append(job)

    @property
    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def due(self, now: datetime, last_interaction: datetime | None = None) -> list[Job]:
        """The jobs due at `now` given the idle signal — pure, no side effects (it does
        NOT advance `last_run`). Callers tick via `tick()`; tests assert the set."""
        return [
            j for j in self._jobs
            if j.cadence.is_due(now, self._last_run.get(j.name), last_interaction)
        ]

    async def tick(self, *, last_interaction: datetime | None = None) -> None:
        """Run one scheduler pass: mark each due job as run, then execute reflex jobs
        in-core / route turn jobs to dispatch. Every job runs under its own guard so
        one failure logs and the scheduler keeps ticking (mirrors the old reflex loop)."""
        now = self._now()
        for job in self.due(now, last_interaction):
            self._last_run[job.name] = now  # mark before running: a failure waits the period, never busy-loops
            try:
                if job.cost_tier is CostTier.REFLEX:
                    await job.run()
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
