"""Story 5.3 — the battery-aware backoff policy (AD-14/AD-9/AD-1).

Pure policy: given a `PowerState` reading, resolve a `BackoffLevel` and from it the
cadence stretch factor + whether a turn job is skipped. Three levels (owner decision 1):
LIVELY (plugged in / charging), EASED (on battery, charge OK/unknown), LOW (on battery,
charge < threshold). No clock, no I/O — the policy is instantaneous over one reading.
The scheduler (test_scheduler.py / test_battery_backoff.py) is the driver that reads
power each tick and applies the stretch + skip.
"""

import pytest

from shelldon.core.power import BackoffLevel, BackoffPolicy, PowerState


def _policy(eased=3.0, low=6.0, threshold=0.20) -> BackoffPolicy:
    return BackoffPolicy(eased_scale=eased, low_scale=low, low_charge_threshold=threshold)


# --- config validation (the 5.1/5.2 fail-fast precedent) ---


def test_rejects_scale_below_one_or_nan():
    """A scale < 1 would SPEED UP cadences on battery (the opposite of backoff); NaN slips
    past a bare comparison. Reject both, for both scales."""
    for bad in (0.0, 0.5, -1.0, float("nan")):
        with pytest.raises(ValueError):
            BackoffPolicy(eased_scale=bad, low_scale=6.0, low_charge_threshold=0.2)
        with pytest.raises(ValueError):
            BackoffPolicy(eased_scale=3.0, low_scale=bad, low_charge_threshold=0.2)


def test_rejects_threshold_out_of_range_or_nan():
    for bad in (0.0, -0.1, 1.5, float("nan")):
        with pytest.raises(ValueError):
            BackoffPolicy(eased_scale=3.0, low_scale=6.0, low_charge_threshold=bad)


def test_rejects_low_scale_below_eased_scale():
    """LOW is a DEEPER backoff than EASED (lower charge ⇒ stretch harder), so low_scale must
    be >= eased_scale. A smaller low_scale would make the deepest tier wake MORE often than
    the middle tier — inverting the battery-saving contract. Equal is allowed."""
    with pytest.raises(ValueError):
        BackoffPolicy(eased_scale=3.0, low_scale=1.5, low_charge_threshold=0.20)
    # equal is fine (boundary)
    BackoffPolicy(eased_scale=3.0, low_scale=3.0, low_charge_threshold=0.20)


# --- level(): the three-tier truth table (AC1, AC2) ---


def test_plugged_in_is_lively_regardless_of_charge():
    """Plugged ⇒ LIVELY even at a low charge — a charging battery is recovering, not
    backing off (owner decision)."""
    assert _policy().level(PowerState(on_battery=False, charge=None)) is BackoffLevel.LIVELY
    assert _policy().level(PowerState(on_battery=False, charge=0.05)) is BackoffLevel.LIVELY
    assert _policy().level(PowerState(on_battery=False, charge=1.0)) is BackoffLevel.LIVELY


def test_on_battery_with_ample_charge_is_eased():
    assert _policy(threshold=0.20).level(PowerState(on_battery=True, charge=0.80)) is BackoffLevel.EASED
    # exactly at the threshold is NOT below it -> still EASED
    assert _policy(threshold=0.20).level(PowerState(on_battery=True, charge=0.20)) is BackoffLevel.EASED


def test_on_battery_below_threshold_is_low():
    assert _policy(threshold=0.20).level(PowerState(on_battery=True, charge=0.10)) is BackoffLevel.LOW


def test_fully_drained_on_battery_is_low():
    """The drained boundary: charge=0.0 on battery is below any positive threshold -> LOW
    (a future boundary-condition change must not silently invert this to EASED)."""
    assert _policy(threshold=0.20).level(PowerState(on_battery=True, charge=0.0)) is BackoffLevel.LOW


def test_on_battery_with_unknown_charge_is_eased_never_low():
    """A missing charge reading must never escalate to the deepest backoff — stay EASED."""
    assert _policy().level(PowerState(on_battery=True, charge=None)) is BackoffLevel.EASED


# --- cadence_scale(): per-level stretch factor (AC1, AC2) ---


def test_cadence_scale_per_level():
    p = _policy(eased=3.0, low=6.0)
    assert p.cadence_scale(BackoffLevel.LIVELY) == 1.0
    assert p.cadence_scale(BackoffLevel.EASED) == 3.0
    assert p.cadence_scale(BackoffLevel.LOW) == 6.0


# --- skips(): the turn-skip matrix (AC1, AC2) ---


def test_lively_never_skips():
    p = _policy()
    assert p.skips(BackoffLevel.LIVELY, essential=True) is False
    assert p.skips(BackoffLevel.LIVELY, essential=False) is False


def test_eased_skips_non_essential_only():
    p = _policy()
    assert p.skips(BackoffLevel.EASED, essential=False) is True   # non-essential turn skipped
    assert p.skips(BackoffLevel.EASED, essential=True) is False   # essential turn still runs


def test_low_skips_everything_including_essential():
    p = _policy()
    assert p.skips(BackoffLevel.LOW, essential=False) is True
    assert p.skips(BackoffLevel.LOW, essential=True) is True
