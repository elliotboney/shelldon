"""core/reflexes — the resident reflex policy (AD-1/AD-5/AD-14, CAP-2).

A pure function that, given the current personality-state and the current time,
returns a **sparse patch over the existing Story 3.1 closed dotted paths** — the
small per-tick drift that makes the pet feel alive between turns (time-of-day mood
drift + idle settling), with no LLM and no I/O.

The *policy* (what to drift) lives here as a pure, deterministic function; the
*driver* (when to run it) lives in `core/runtime.py` as a periodic in-core tick.
That split is the AD-14 seam: Epic 5's scheduler subsumes the tick by calling this
same function as a cost-tier "reflex job" — unchanged behavior, no forward
dependency. It is intentionally minimal — gentle nudges with hard clamps, NOT an
affect engine.
"""

import logging
from datetime import datetime

from shelldon.core.state import PersonalityState

log = logging.getLogger("shelldon.core.reflexes")

_VALENCE_RANGE = (-1.0, 1.0)
_AROUSAL_RANGE = (-1.0, 1.0)
_ENERGY_RANGE = (0.0, 1.0)

DRIFT_RATE = 0.1
#: A nudge smaller than this is treated as no drift, so an at-rest tick returns {}
#: and marks nothing dirty — bounds reflex write-churn at the policy level (NFR7).
#: Kept in this pure function (not the driver) so Epic 5's scheduler subsuming it
#: inherits the same no-churn behavior unchanged (AC3).
EPSILON = 1e-3

IDLE_SETTLE_AFTER_S = 300.0
RESTING_VALENCE = 0.0
RESTING_ENERGY = 0.5


def _clamp(value: float, lo_hi: tuple[float, float]) -> float:
    lo, hi = lo_hi
    return lo if value < lo else hi if value > hi else value


def _toward(current: float, target: float, lo_hi: tuple[float, float]) -> float:
    """One gentle, clamped step from `current` toward `target`."""
    return _clamp(current + (target - current) * DRIFT_RATE, lo_hi)


def _time_of_day_arousal_target(now: datetime) -> float:
    """Calm at night, lively midday, neutral at the shoulders (UTC hour buckets)."""
    hour = now.hour
    if hour >= 22 or hour < 6:
        return -0.5  # night — calm/sleepy
    if 10 <= hour < 16:
        return 0.5  # midday — lively
    return 0.0  # morning / evening — neutral


def _idle_seconds(state: PersonalityState, now: datetime) -> float | None:
    """Seconds since the last interaction, or None when there is no usable signal.

    `now` and stored timestamps are tz-aware UTC (`_mark_interaction` writes
    `datetime.now(UTC).isoformat()`). An unparseable value, a non-string, or a
    tz-naive timestamp (which can't be subtracted from an aware `now`) is treated as
    'no idle signal' and warned — never raised."""
    if state.last_interaction is None:
        return None
    try:
        return (now - datetime.fromisoformat(state.last_interaction)).total_seconds()
    except (ValueError, TypeError) as exc:
        log.warning(
            "unusable last_interaction %r (%s); ignoring idle drift this tick",
            state.last_interaction,
            exc,
        )
        return None


def compute_reflex_patch(state: PersonalityState, now: datetime) -> dict:
    """Return the sparse patch this reflex tick would apply — pure, no mutation, no
    I/O. Keys are a subset of the Story 3.1 closed writable paths; an empty dict
    means 'nothing to drift this tick'.

    Two reflexes: (1) arousal drifts toward a time-of-day target; (2) once idle past
    a threshold, valence and energy settle toward their resting baselines.
    """
    patch: dict = {}

    new_arousal = _toward(state.mood.arousal, _time_of_day_arousal_target(now), _AROUSAL_RANGE)
    if abs(new_arousal - state.mood.arousal) > EPSILON:
        patch["mood.arousal"] = new_arousal

    idle = _idle_seconds(state, now)
    if idle is not None and idle >= IDLE_SETTLE_AFTER_S:
        new_valence = _toward(state.mood.valence, RESTING_VALENCE, _VALENCE_RANGE)
        if abs(new_valence - state.mood.valence) > EPSILON:
            patch["mood.valence"] = new_valence
        new_energy = _toward(state.energy, RESTING_ENERGY, _ENERGY_RANGE)
        if abs(new_energy - state.energy) > EPSILON:
            patch["energy"] = new_energy

    return patch
