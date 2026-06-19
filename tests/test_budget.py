"""Story 5.2 — the turn-job spend gate (AD-9/AD-14/AD-1).

Pure policy: given the persisted budget ledger, the current time, and a job's cost,
decide whether a scheduler-initiated turn may be admitted. Two orthogonal gates — a
daily turn-COUNT budget (default 12) and a minimum-interval cooldown (default 30 min) —
plus a calendar-day rollover in the OWNER'S LOCAL timezone (owner decision 2026-06-18).

Deterministic clock injection, never sleep anchors (Epic 2 retro #1 / the reflex+scheduler
test pattern). The runtime applies `admission_patch` through the single-writer apply_patch.
"""

from datetime import UTC, datetime, timedelta

import pytest

from shelldon.core.budget import BudgetGate, Decision
from shelldon.core.state import TurnBudget

_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
_TODAY = _NOW.astimezone().date().isoformat()  # the owner-local date of _NOW (tz-independent test)


def _gate(daily=12, cooldown=1800.0) -> BudgetGate:
    return BudgetGate(daily_turn_budget=daily, turn_cooldown=cooldown)


# --- config validation (the 5.1 cadence-guard precedent) ---


def test_rejects_nonpositive_or_nan_config():
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            BudgetGate(daily_turn_budget=bad, turn_cooldown=1800.0)
    for bad in (0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            BudgetGate(daily_turn_budget=12, turn_cooldown=bad)


# --- ADMIT / DEFER / SKIP ---


def test_admit_on_a_fresh_ledger():
    assert _gate().evaluate(TurnBudget(), _NOW, cost=1) is Decision.ADMIT


def test_defer_within_the_cooldown_window():
    b = TurnBudget(date=_TODAY, turns_used=1, last_turn_at=(_NOW - timedelta(seconds=100)).isoformat())
    assert _gate(cooldown=1800.0).evaluate(b, _NOW, cost=1) is Decision.DEFER


def test_admit_once_the_cooldown_has_elapsed():
    b = TurnBudget(date=_TODAY, turns_used=1, last_turn_at=(_NOW - timedelta(seconds=2000)).isoformat())
    assert _gate(cooldown=1800.0).evaluate(b, _NOW, cost=1) is Decision.ADMIT


def test_skip_when_the_daily_budget_is_exhausted():
    b = TurnBudget(date=_TODAY, turns_used=12, last_turn_at=(_NOW - timedelta(seconds=9999)).isoformat())
    assert _gate(daily=12).evaluate(b, _NOW, cost=1) is Decision.SKIP


def test_skip_takes_precedence_over_cooldown_when_exhausted():
    """Exhausted budget reports SKIP even inside the cooldown window — it won't run today
    regardless, so 'skip' is the accurate decision (not 'defer and retry')."""
    b = TurnBudget(date=_TODAY, turns_used=12, last_turn_at=(_NOW - timedelta(seconds=10)).isoformat())
    assert _gate(daily=12, cooldown=1800.0).evaluate(b, _NOW, cost=1) is Decision.SKIP


# --- per-job cost weight (owner decision 3: dream turns count heavier) ---


def test_cost_weight_consumes_proportionally():
    gate = _gate(daily=12, cooldown=1.0)
    after_cooldown = (_NOW - timedelta(seconds=10)).isoformat()
    b_room = TurnBudget(date=_TODAY, turns_used=10, last_turn_at=after_cooldown)
    assert gate.evaluate(b_room, _NOW, cost=2) is Decision.ADMIT  # 10 + 2 == 12, fits

    b_tight = TurnBudget(date=_TODAY, turns_used=10, last_turn_at=after_cooldown)
    assert gate.evaluate(b_tight, _NOW, cost=3) is Decision.SKIP  # 10 + 3 == 13 > 12, even though used < cap


# --- local-day rollover (owner decision 4) ---


def test_rollover_resets_used_on_a_new_local_day():
    """Yesterday's exhausted count does not block today — a different stored date means
    today's effective usage is 0."""
    b = TurnBudget(date="2020-01-01", turns_used=12, last_turn_at="2020-01-01T00:00:00+00:00")
    assert _gate(daily=12).evaluate(b, _NOW, cost=1) is Decision.ADMIT


# --- admission_patch records the spend ---


def test_admission_patch_records_today_count_and_stamp():
    patch = _gate().admission_patch(TurnBudget(date=_TODAY, turns_used=3), _NOW, cost=1)
    assert patch == {
        "budget.date": _TODAY,
        "budget.turns_used": 4,
        "budget.last_turn_at": _NOW.isoformat(),
    }


def test_admission_patch_resets_count_on_rollover():
    patch = _gate().admission_patch(TurnBudget(date="2020-01-01", turns_used=12), _NOW, cost=2)
    assert patch["budget.date"] == _TODAY
    assert patch["budget.turns_used"] == 2  # yesterday's 12 dropped; fresh day starts at cost


# --- defensive: a garbage cooldown stamp is ignored, never raised ---


def test_unusable_last_turn_at_is_treated_as_no_cooldown():
    for bad in ("not-a-timestamp", "2026-06-18T12:00:00"):  # garbage, then tz-naive
        b = TurnBudget(date=_TODAY, turns_used=0, last_turn_at=bad)
        assert _gate().evaluate(b, _NOW, cost=1) is Decision.ADMIT


def test_future_last_turn_at_recovers_instead_of_deferring_forever():
    """A future stamp (backward clock jump / corrupt checkpoint) must NOT DEFER every turn
    job forever — it's treated as no active cooldown so autonomy recovers."""
    future = (_NOW + timedelta(hours=5)).isoformat()
    b = TurnBudget(date=_TODAY, turns_used=0, last_turn_at=future)
    assert _gate().evaluate(b, _NOW, cost=1) is Decision.ADMIT
