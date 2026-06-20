"""Story 7.5 — the affect→patch policy (`core/reactions.py`).

Pure, LLM-free, clockless: maps a semantic affect `EventKind` (a plugin's NUDGE_*) to a
sparse, clamped patch over the closed mood paths. Core owns the magnitude (the table lives
here, not in any plugin). Mirrors `compute_reflex_patch`'s shape so it feeds `apply_patch`.
"""

import pytest

from shelldon.contracts import EventKind
from shelldon.core.reactions import compute_nudge_patch


def test_positive_nudges_valence_up():
    assert compute_nudge_patch(EventKind.NUDGE_POSITIVE, 0.0, 0.0) == {"mood.valence": pytest.approx(0.3)}


def test_negative_nudges_valence_down():
    assert compute_nudge_patch(EventKind.NUDGE_NEGATIVE, 0.0, 0.0) == {"mood.valence": pytest.approx(-0.3)}


def test_excited_nudges_both_axes():
    patch = compute_nudge_patch(EventKind.NUDGE_EXCITED, 0.0, 0.0)
    assert patch == {"mood.valence": pytest.approx(0.1), "mood.arousal": pytest.approx(0.3)}


def test_calm_nudges_arousal_down():
    assert compute_nudge_patch(EventKind.NUDGE_CALM, 0.0, 0.0) == {"mood.arousal": pytest.approx(-0.3)}


def test_patch_is_absolute_valued_not_a_delta():
    # Like compute_reflex_patch: the value is the NEW absolute coordinate, ready for apply_patch.
    patch = compute_nudge_patch(EventKind.NUDGE_POSITIVE, 0.5, 0.0)
    assert patch == {"mood.valence": pytest.approx(0.8)}


def test_clamps_at_the_upper_bound():
    # +0.3 from 0.9 would be 1.2 -> clamped to the [-1, 1] ceiling.
    assert compute_nudge_patch(EventKind.NUDGE_POSITIVE, 0.9, 0.0) == {"mood.valence": pytest.approx(1.0)}


def test_clamps_at_the_lower_bound():
    assert compute_nudge_patch(EventKind.NUDGE_CALM, 0.0, -0.9) == {"mood.arousal": pytest.approx(-1.0)}


def test_no_op_at_the_bound_returns_none():
    # Already pinned at the ceiling: the nudge can't move it, so there's nothing to apply.
    assert compute_nudge_patch(EventKind.NUDGE_POSITIVE, 1.0, 0.0) is None


def test_unknown_kind_returns_none():
    # A non-affect kind core happens to see (e.g. its own MESSAGE_ANSWERED) maps to nothing.
    assert compute_nudge_patch(EventKind.MESSAGE_ANSWERED, 0.0, 0.0) is None
    assert compute_nudge_patch(EventKind.BUTTON_PRESSED, 0.0, 0.0) is None
