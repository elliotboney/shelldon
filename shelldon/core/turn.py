"""Turn fencing (AD-12): core admits a Result only for the in-flight turn.

A `Result` whose `turn_id` is closed, superseded, unknown, or absent is discarded
— so a late or zombie worker can't pollute the next turn or race a fallback. Turn
close is idempotent. Supersession/timeout sophistication is the arbiter's (1.8).
"""

from collections import deque

from shelldon.contracts import Envelope


class TurnFence:
    """Tracks the one open `turn_id` and a bounded history of closed ones."""

    def __init__(self, max_closed: int = 256):
        self.current: str | None = None
        self._closed: set[str] = set()
        self._closed_order: deque[str] = deque(maxlen=max_closed)

    @property
    def is_idle(self) -> bool:
        """True when no turn is open (ready for the next turn) — lets callers/tests
        assert on intent rather than the `current` field directly."""
        return self.current is None

    def open(self, turn_id: str) -> None:
        """Open a turn. A different turn already open is superseded (closed)."""
        if self.current is not None and self.current != turn_id:
            self.close(self.current)
        self.current = turn_id

    def close(self, turn_id: str) -> None:
        """Close a turn (idempotent). The current turn, if it matches, ends."""
        if self.current == turn_id:
            self.current = None
        if turn_id not in self._closed:
            if len(self._closed_order) == self._closed_order.maxlen:
                self._closed.discard(self._closed_order[0])
            self._closed_order.append(turn_id)
            self._closed.add(turn_id)

    def accept(self, env: Envelope) -> bool:
        """True only for a Result on the currently-open turn; else discard."""
        tid = env.turn_id
        return tid is not None and tid == self.current and tid not in self._closed
