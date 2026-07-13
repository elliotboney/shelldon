"""The single-knob turn-timeout chain (SHELLDON_TURN_TIMEOUT → W < R < T).

The module constants freeze at import, so we exercise the resolver + offset math directly:
whatever T resolves to, the derived R/W must preserve the coherent-timeout ordering that
`test_resilience.py::test_timeout_chain_is_coherent` asserts on the live constants."""

import shelldon.timeouts as to


def _chain(t: float) -> tuple[float, float, float]:
    """Reproduce the module's derivation for an arbitrary resolved T → (W, R, T)."""
    return (t - to._COMPLETION_MARGIN, t - to._REAP_MARGIN, t)


def test_unset_reproduces_historical_defaults(monkeypatch):
    monkeypatch.delenv("SHELLDON_TURN_TIMEOUT", raising=False)
    t = to._resolve_turn_timeout()
    assert t == 30.0
    assert _chain(t) == (25.0, 28.0, 30.0)  # W, R, T unchanged from before the knob


def test_valid_env_moves_whole_chain_preserving_order(monkeypatch):
    monkeypatch.setenv("SHELLDON_TURN_TIMEOUT", "60")
    t = to._resolve_turn_timeout()
    w, r, t = _chain(t)
    assert (w, r, t) == (55.0, 58.0, 60.0)
    assert w < r < t


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SHELLDON_TURN_TIMEOUT", "not-a-number")
    assert to._resolve_turn_timeout() == 30.0


def test_below_floor_is_clamped(monkeypatch):
    monkeypatch.setenv("SHELLDON_TURN_TIMEOUT", "5")
    t = to._resolve_turn_timeout()
    assert t == to._MIN_TURN_TIMEOUT
    w, r, _ = _chain(t)
    # Clamp keeps W above the 5s outbound-write timeout AND the W < R < T ordering.
    assert 5.0 < w < r < t
