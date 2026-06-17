"""Arbiter skeleton (AD-9): the ≤1-worker-in-flight bound.

Core decides whether a turn may begin; the fork-server (worker/) executes it. This
is only the bound — event coalescing, cooldown, credit/battery budget, and
degrade-to-reflex (the full AD-9 arbiter) arrive in Story 1.8 / Epic 2 / Epic 5.
"""


class Arbiter:
    """Admits at most one worker turn at a time."""

    def __init__(self):
        self.worker_in_flight = False

    def try_begin(self) -> bool:
        """Reserve the single turn slot. False if one is already in flight."""
        if self.worker_in_flight:
            return False
        self.worker_in_flight = True
        return True

    def end(self) -> None:
        """Release the slot — call on worker exit OR a failed spawn."""
        self.worker_in_flight = False
