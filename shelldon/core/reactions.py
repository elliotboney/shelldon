"""core/reactions — the plugin-affect → mood-patch policy (AD-1/AD-5, Story 7.5).

A pure function that maps a semantic affect `EventKind` (a plugin's NUDGE_*) to a
**sparse, clamped patch over the Story 3.1 closed mood paths** — the bounded mood move a
single nudge applies. Core owns the magnitude (the affect→delta table lives HERE, never in
a plugin): a plugin emits a *meaning* and core decides how far the soul moves, so AD-5 keeps
core the sole authority over the pet's affect dynamics, not just the sole writer.

Mirrors `core/reflexes.py`: the *policy* (how much each affect moves mood) is this pure,
clockless, I/O-free function; the *driver* (the per-kind cooldown, the apply, the face
re-render) lives in `core/runtime.py`. The returned patch is absolute-valued and shaped
exactly like `compute_reflex_patch`'s output, so it feeds `state.apply_patch` directly.
"""

from shelldon.contracts import EventKind

_VALENCE_RANGE = (-1.0, 1.0)
_AROUSAL_RANGE = (-1.0, 1.0)

#: The closed affect→(valence_delta, arousal_delta) map (Story 7.5). Magnitudes honor 7-4
#: D3's owner-locked 0.3 scale, recast onto clean single-emphasis affect kinds: POSITIVE and
#: NEGATIVE move valence; EXCITED is arousal-led with a small valence lift; CALM lowers
#: arousal. A new affect flavor is one more row here (a closed-enum + a table entry) — never
#: a free-numeric payload from a plugin.
_NUDGE_DELTAS: dict[EventKind, tuple[float, float]] = {
    EventKind.NUDGE_POSITIVE: (0.3, 0.0),
    EventKind.NUDGE_NEGATIVE: (-0.3, 0.0),
    EventKind.NUDGE_EXCITED: (0.1, 0.3),
    EventKind.NUDGE_CALM: (0.0, -0.3),
}


def _clamp(value: float, lo_hi: tuple[float, float]) -> float:
    lo, hi = lo_hi
    return lo if value < lo else hi if value > hi else value


def compute_nudge_patch(kind: EventKind, valence: float, arousal: float) -> dict | None:
    """Return the sparse, clamped patch a nudge of `kind` applies to the current
    `(valence, arousal)`, or None when there is nothing to apply.

    None means either: `kind` is not an affect kind (a non-NUDGE event core happened to
    see — e.g. its own MESSAGE_ANSWERED), or every axis the nudge would move is already at
    its bound (a no-op tick, like `compute_reflex_patch`'s EPSILON guard). Otherwise the
    patch maps the closed mood paths to their NEW absolute, in-range coordinates. Pure: no
    mutation, no clock, no I/O.
    """
    deltas = _NUDGE_DELTAS.get(kind)
    if deltas is None:
        return None
    dv, da = deltas
    patch: dict = {}
    new_v = _clamp(valence + dv, _VALENCE_RANGE)
    if new_v != valence:
        patch["mood.valence"] = new_v
    new_a = _clamp(arousal + da, _AROUSAL_RANGE)
    if new_a != arousal:
        patch["mood.arousal"] = new_a
    return patch or None
