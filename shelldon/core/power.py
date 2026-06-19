"""core/power — the battery-aware backoff policy (AD-14/AD-9/AD-1).

AD-14 makes the scheduler battery-aware: it reads PiSugar2 power state and "stretches
cadences / skips non-essential LLM turns on battery or low charge, livelier when plugged
in." This module is the pure POLICY half — it maps one power reading to a backoff level,
and from that level to a cadence stretch factor + a turn-skip verdict. The driver (read
power each tick, apply the stretch + skip) lives in `core/scheduler.py` — same policy/driver
split as `core/budget.py` / `core/reflexes.py`. The policy is instantaneous: no clock, no I/O.

The real PiSugar2 read is a **plugin-host plugin (AD-8, Epic 7)** that surfaces power to
core; plugin-host is not built yet, so the scheduler reads an **injected `PowerState`**
that defaults to a plugged-in stub. Swapping the stub for the real cached reading later is
a zero-policy-change edit.

LLM-free (AD-1): imports only stdlib enum + msgspec for the reading struct.
"""

import enum

import msgspec


class PowerState(msgspec.Struct, frozen=True):
    """One power reading. RAM-only (like `PersonalityState`) — NOT a `contracts/` bus type
    (the plugin-host → core power envelope is Epic 7). `charge` is a 0.0–1.0 fraction, or
    `None` when the source can't report it (treated conservatively — never the deepest
    backoff). The default is the plugged-in stub the scheduler uses until Epic 7 wires a
    real reader."""

    on_battery: bool = False
    charge: float | None = None


class BackoffLevel(enum.Enum):
    """How hard the pet backs off, derived from the power reading (owner decision 1)."""

    LIVELY = "lively"  # plugged in / charging — normal cadences, nothing skipped
    EASED = "eased"    # on battery, charge OK/unknown — stretch cadences, skip non-essential turns
    LOW = "low"        # on battery, charge < threshold — deeper stretch, skip ALL turns


class BackoffPolicy:
    """Pure policy: power reading → level → (cadence stretch, turn-skip). Holds only config
    (the per-level stretch factors + the low-charge threshold); no mutable state."""

    def __init__(self, *, eased_scale: float = 3.0, low_scale: float = 6.0, low_charge_threshold: float = 0.20) -> None:
        # `not (x >= 1.0)` rejects a scale below 1 (which would SHORTEN cadences on battery —
        # the opposite of backoff) AND NaN (NaN >= 1.0 is False) — the 5.1/5.2 fail-fast guard.
        if not (eased_scale >= 1.0):
            raise ValueError(f"eased_scale must be >= 1.0, got {eased_scale!r}")
        if not (low_scale >= 1.0):
            raise ValueError(f"low_scale must be >= 1.0, got {low_scale!r}")
        # LOW is a DEEPER backoff than EASED (lower charge ⇒ stretch harder), so its scale
        # must be at least the EASED scale. A smaller low_scale would make the deepest tier
        # fire MORE often than the middle tier — inverting the battery-saving contract.
        if not (low_scale >= eased_scale):
            raise ValueError(f"low_scale ({low_scale!r}) must be >= eased_scale ({eased_scale!r})")
        # A threshold outside (0, 1] is meaningless as a charge fraction; the chained compare
        # also rejects NaN (every NaN comparison is False).
        if not (0.0 < low_charge_threshold <= 1.0):
            raise ValueError(f"low_charge_threshold must be in (0, 1], got {low_charge_threshold!r}")
        self.eased_scale = eased_scale
        self.low_scale = low_scale
        self.low_charge_threshold = low_charge_threshold

    def level(self, power: PowerState) -> BackoffLevel:
        """Plugged ⇒ LIVELY regardless of charge (charging = recovering, not backing off).
        On battery: LOW only on a KNOWN charge below the threshold; otherwise (ample, or an
        unknown reading) EASED — a missing reading never escalates to the deepest backoff."""
        if not power.on_battery:
            return BackoffLevel.LIVELY
        if power.charge is not None and power.charge < self.low_charge_threshold:
            return BackoffLevel.LOW
        return BackoffLevel.EASED

    def cadence_scale(self, level: BackoffLevel) -> float:
        """The multiplier applied to a job's cadence period — bigger = fires less often."""
        if level is BackoffLevel.EASED:
            return self.eased_scale
        if level is BackoffLevel.LOW:
            return self.low_scale
        return 1.0  # LIVELY — normal cadences

    def skips(self, level: BackoffLevel, *, essential: bool) -> bool:
        """Whether a TURN-tier job is skipped at this level (the caller applies this only to
        turn jobs — reflex jobs are never skipped, only stretched). LIVELY skips nothing;
        EASED skips non-essential turns; LOW skips all turns including essential ones."""
        if level is BackoffLevel.LIVELY:
            return False
        if level is BackoffLevel.LOW:
            return True
        return not essential  # EASED
