"""Single source of truth for the coherent turn-timeout chain (Story 5.0 invariant
W < R < T): worker self-report (W) < fork-server reap SIGKILL (R) < core degrade (T).

ONE knob drives all three — `SHELLDON_TURN_TIMEOUT` (seconds, = T) — so config can never
break the ordering. Read once at import; the fork-server preloads these modules in the
parent, and child workers inherit the same env, so every process resolves the same T.
Raising T on a slow-brain host (e.g. the Pi, where GLM latency spikes past the 30s default
and the worker's 25s wall degrades the turn to "…can't think right now…" before the real
answer lands) moves the whole chain up together.

Defaults (T=30) reproduce the historical 25/28/30 exactly. See
`tests/test_resilience.py::test_timeout_chain_is_coherent`."""

import logging
import os

log = logging.getLogger("shelldon.timeouts")

#: Core degrade timeout (T) when SHELLDON_TURN_TIMEOUT is unset/invalid.
DEFAULT_TURN_TIMEOUT = 30.0

#: Offsets below T that reproduce the historical chain at the default: R = T-2, W = T-5.
_REAP_MARGIN = 2.0
_COMPLETION_MARGIN = 5.0

#: Floor on T so the derived worker window (W = T-5) stays comfortably above the 5s
#: outbound-write timeout (`_RESULT_WRITE_TIMEOUT_S`) — keeps W < R < T AND write < W.
_MIN_TURN_TIMEOUT = 15.0


def _resolve_turn_timeout() -> float:
    raw = os.getenv("SHELLDON_TURN_TIMEOUT")
    if not raw:
        return DEFAULT_TURN_TIMEOUT
    try:
        t = float(raw)
    except ValueError:
        log.warning("ignoring invalid SHELLDON_TURN_TIMEOUT=%r; using %.0fs", raw, DEFAULT_TURN_TIMEOUT)
        return DEFAULT_TURN_TIMEOUT
    if t < _MIN_TURN_TIMEOUT:
        log.warning("SHELLDON_TURN_TIMEOUT=%.0fs below floor; clamping to %.0fs", t, _MIN_TURN_TIMEOUT)
        return _MIN_TURN_TIMEOUT
    return t


#: T — core waits this long for a worker Result before degrading.
TURN_TIMEOUT = _resolve_turn_timeout()
#: R — fork-server SIGKILL-reclaims a wedged child (strictly < T).
REAP_TIMEOUT = TURN_TIMEOUT - _REAP_MARGIN
#: W — worker self-reports a failure Result if the broker doesn't answer (strictly < R).
COMPLETION_TIMEOUT = TURN_TIMEOUT - _COMPLETION_MARGIN
