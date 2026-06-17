"""Arbiter (AD-9): the ≤1-worker-in-flight bound + single-slot coalescing.

Core decides whether a turn may begin; the fork-server (worker/) executes it.
While a turn is in flight, further owner messages do NOT spawn a second worker and
are NOT dropped — they coalesce into a SINGLE pending catch-up slot, and the next
turn folds in everything accumulated since it started (AD-9: "a single pending
catch-up slot — never a growing backlog of turns"). Cooldown, credit/battery
budget, and the full degrade-to-reflex chain are Epic 2 / Epic 5.

Pure policy — no I/O, no asyncio. The core runtime (single-consumer loop) calls
it, so access is already serial; no lock is needed for that design.
"""


class Arbiter:
    """Admits ≤1 turn; coalesces concurrent events into one catch-up turn."""

    def __init__(self):
        self.worker_in_flight = False
        self._pending: list[str] = []

    def submit(self, text: str) -> str | None:
        """Admit an owner message.

        Returns the prompt to start a turn NOW if the slot is free (and reserves
        it); returns None if a turn is already in flight, folding `text` into the
        single pending catch-up slot (never dropped — AC2).
        """
        if self.worker_in_flight:
            self._pending.append(text)
            return None
        self.worker_in_flight = True
        return text

    def complete(self) -> str | None:
        """End the in-flight turn and maybe drive ONE catch-up turn.

        If messages accumulated during the turn, re-reserve the slot, fold them
        into one prompt (newline-joined), clear pending, and return it (exactly one
        catch-up turn). If nothing pending, release the slot and return None.
        Minimal merge shaping; richer merge/dedup is later.
        """
        if self._pending:
            folded = "\n".join(self._pending)
            self._pending.clear()
            self.worker_in_flight = True  # re-reserve for the catch-up turn
            return folded
        self.worker_in_flight = False
        return None

    def reset(self) -> None:
        """Release the slot and discard any pending catch-up.

        Used when the runtime fails to actually start a turn it admitted (a spawn
        error) — without this the slot stays reserved forever and every later
        message silently coalesces into a pending slot that never flushes. The
        dropped catch-up is accepted degraded behavior; guaranteed redelivery for
        a failed-to-start turn is Epic 2.
        """
        self.worker_in_flight = False
        self._pending.clear()
