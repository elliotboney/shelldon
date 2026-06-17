"""Core runtime unit tests — the turn-start failure path (review P1).

A spawn that raises must NOT leave the turn guards stuck: the fence has to close
and the arbiter slot must release, or every later message coalesces into a pending
slot that never flushes (no future turn can ever start).
"""

import pytest

from shelldon.core.runtime import Core
from shelldon.worker.forkserver import WorkerBusyError


class _FailingSpawner:
    """Ready immediately; every spawn raises. `exc` selects the failure type."""

    def __init__(self, exc):
        self._exc = exc

    async def ready(self) -> None:
        pass

    async def spawn_turn(self, turn_id, prompt):
        raise self._exc

    async def reap_current(self) -> None:  # pragma: no cover - never reached
        pass


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("fork failed"), WorkerBusyError("mechanical guard still held")],
)
async def test_start_turn_releases_guards_on_spawn_failure(sock_path, exc):
    core = Core(sock_path, _FailingSpawner(exc))

    prompt = core.arbiter.submit("hello")   # admit a turn (worker_in_flight=True)
    assert prompt == "hello"
    await core._start_turn(prompt)           # spawn raises inside here

    # Both guards released — not stuck:
    assert core.arbiter.worker_in_flight is False
    assert core.fence.current is None
    # A new turn can be admitted and started, proving nothing is wedged:
    assert core.arbiter.submit("again") == "again"
