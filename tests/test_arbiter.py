"""AC2: the arbiter admits at most one turn in flight (pure ≤1 policy)."""

from shelldon.core.arbiter import Arbiter


def test_one_turn_at_a_time():
    a = Arbiter()
    assert a.try_begin() is True           # first turn admitted
    assert a.worker_in_flight is True
    assert a.try_begin() is False          # second refused while one is in flight


def test_end_releases():
    a = Arbiter()
    a.try_begin()
    a.end()
    assert a.worker_in_flight is False
    assert a.try_begin() is True           # a new turn can begin after release
