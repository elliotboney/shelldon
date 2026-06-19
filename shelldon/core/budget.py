"""core/budget — the scheduler-turn spend gate (AD-9/AD-14/AD-1).

The owner's concern is "don't quietly burn my API credits." AD-9 makes the arbiter the
single gate for turns; this module is the pure POLICY half of the turn-job gate it
applies to scheduler-proposed turns: a **daily turn-COUNT budget** (cap total self-driven
spend) and a **minimum-interval cooldown** (stop a proactive stampede), both of which must
pass to admit. The driver (apply the patch + admit through the arbiter + spawn) lives in
`core/dispatch.py` — same policy/driver split as `core/reflexes.py`.

Two owner decisions are baked in: the daily count resets on the owner's **LOCAL** calendar
day (not UTC), and a job carries a **cost weight** so a heavier turn (a future dream turn)
counts for several against the cap. Cost is a turn COUNT, not dollars/tokens — true
$-accounting needs the broker's token detail and is deferred.

LLM-free (AD-1): imports only stdlib datetime + the state ledger struct.
"""

import enum
import logging
from datetime import datetime

from shelldon.core.state import TurnBudget

log = logging.getLogger("shelldon.core.budget")


class Decision(enum.Enum):
    """The gate's verdict for a due turn job."""

    ADMIT = "admit"  # slot/cooldown/budget all allow — start the turn
    DEFER = "defer"  # within the cooldown window — re-proposed next cadence
    SKIP = "skip"  # daily budget exhausted — don't run today (AC2)


def _local_date(now: datetime) -> str:
    """The owner-local calendar date of `now` as ISO. The injected clock is tz-aware
    UTC; `astimezone()` (no arg) converts to the system local zone so 'daily' means the
    owner's day (decision 4)."""
    return now.astimezone().date().isoformat()


def _seconds_since(stamp: str | None, now: datetime) -> float | None:
    """Seconds since `stamp`, or None when there is no usable signal. An unparseable or
    tz-naive stamp (e.g. a hand-edited checkpoint) is treated as 'no active cooldown' and
    warned — never raised. Mirrors `reflexes._idle_seconds`."""
    if stamp is None:
        return None
    try:
        elapsed = (now - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError) as exc:
        log.warning("unusable last_turn_at %r (%s); treating as no active cooldown", stamp, exc)
        return None
    if elapsed < 0:
        # A future stamp (a backward clock jump or a hand-edited checkpoint) would make the
        # cooldown DEFER every turn job until wall-clock caught up — silently wedging
        # autonomy. Treat it as 'no active cooldown' and warn, so the pet recovers.
        log.warning("last_turn_at %r is in the future (%.0fs ahead); treating as no active cooldown",
                    stamp, -elapsed)
        return None
    return elapsed


class BudgetGate:
    """Pure policy: decide whether a scheduler turn may be admitted, and produce the
    ledger patch that records an admission. Holds only config (the daily cap + cooldown);
    the mutable ledger lives in `PersonalityState.budget` (single writer, AD-5)."""

    def __init__(self, *, daily_turn_budget: int, turn_cooldown: float) -> None:
        # `not (x > 0)` rejects zero/negative AND NaN (the 5.1 cadence-guard precedent).
        if not (daily_turn_budget > 0):
            raise ValueError(f"daily_turn_budget must be positive, got {daily_turn_budget!r}")
        if not (turn_cooldown > 0):
            raise ValueError(f"turn_cooldown must be positive, got {turn_cooldown!r}")
        self.daily_turn_budget = daily_turn_budget
        self.turn_cooldown = turn_cooldown

    def _used_today(self, budget: TurnBudget, now: datetime) -> int:
        """Turns spent on the current local day — 0 after a day rollover (the stored
        date no longer matches today), so yesterday's count never blocks today."""
        return budget.turns_used if budget.date == _local_date(now) else 0

    def evaluate(self, budget: TurnBudget, now: datetime, *, cost: int = 1) -> Decision:
        """SKIP if admitting `cost` would exceed the daily cap (reported even inside the
        cooldown — it won't run today regardless); else DEFER if still inside the
        cooldown; else ADMIT."""
        if self._used_today(budget, now) + cost > self.daily_turn_budget:
            return Decision.SKIP
        since = _seconds_since(budget.last_turn_at, now)
        if since is not None and since < self.turn_cooldown:
            return Decision.DEFER
        return Decision.ADMIT

    def admission_patch(self, budget: TurnBudget, now: datetime, *, cost: int = 1) -> dict:
        """The single-writer patch recording one admission: stamp today's local date,
        add `cost` to today's usage (after rollover reset), and set the cooldown stamp."""
        return {
            "budget.date": _local_date(now),
            "budget.turns_used": self._used_today(budget, now) + cost,
            "budget.last_turn_at": now.isoformat(),
        }
