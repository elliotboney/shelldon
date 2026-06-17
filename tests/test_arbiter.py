"""AD-9: the arbiter admits ≤1 turn and coalesces events into ONE pending slot.

Pure policy (no I/O): `submit` decides whether a message starts a turn now or
folds into the single catch-up slot; `complete` releases the slot and drives at
most one folded catch-up turn — never a growing backlog (AC2).
"""

from shelldon.core.arbiter import Arbiter


def test_submit_when_free_returns_prompt_and_marks_in_flight():
    a = Arbiter()
    assert a.submit("hello") == "hello"   # free -> start now, return the prompt
    assert a.worker_in_flight is True


def test_submit_when_busy_coalesces_and_returns_none():
    a = Arbiter()
    a.submit("first")                     # turn in flight
    assert a.submit("second") is None     # not dropped — folded into pending
    assert a.worker_in_flight is True     # still exactly one in flight


def test_complete_with_pending_re_reserves_and_returns_folded_prompt():
    a = Arbiter()
    a.submit("first")
    a.submit("second")
    folded = a.complete()                 # release, then drive one catch-up turn
    assert folded == "second"
    assert a.worker_in_flight is True     # re-reserved for the catch-up turn


def test_complete_with_nothing_releases_and_returns_none():
    a = Arbiter()
    a.submit("only")
    assert a.complete() is None           # nothing pending -> just release
    assert a.worker_in_flight is False


def test_two_submits_during_one_turn_fold_into_one_next_prompt():
    a = Arbiter()
    a.submit("turn-start")                # turn A in flight
    assert a.submit("A") is None          # both fold into the single pending slot
    assert a.submit("B") is None
    folded = a.complete()                 # exactly ONE catch-up turn, both folded in
    assert folded == "A\nB"
    assert a.complete() is None           # that catch-up turn drains the slot — no backlog
    assert a.worker_in_flight is False
