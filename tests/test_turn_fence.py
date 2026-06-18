"""AC3: core fences Results by turn_id — late/closed/unknown turns are discarded."""

from shelldon.contracts import Actor, Envelope, MsgKind, Result
from shelldon.core.turn import TurnFence


def _result_env(turn_id):
    return Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=True, payload="hi"), turn_id=turn_id,
    )


def test_accepts_current_turn():
    f = TurnFence()
    f.open("t1")
    assert f.accept(_result_env("t1")) is True


def test_rejects_closed_turn():
    f = TurnFence()
    f.open("t1")
    f.close("t1")
    assert f.accept(_result_env("t1")) is False  # late Result for a closed turn


def test_rejects_unknown_or_none_turn():
    f = TurnFence()
    f.open("t1")
    assert f.accept(_result_env("t2")) is False   # never-opened turn
    assert f.accept(_result_env(None)) is False    # no turn id


def test_rejects_superseded_turn():
    f = TurnFence()
    f.open("t1")
    f.open("t2")  # t1 superseded by a new turn
    assert f.accept(_result_env("t1")) is False
    assert f.accept(_result_env("t2")) is True


def test_close_is_idempotent():
    f = TurnFence()
    f.open("t1")
    f.close("t1")
    f.close("t1")  # closing twice is safe
    assert f.accept(_result_env("t1")) is False


def test_closed_set_eviction_is_bounded():
    cap = 3
    f = TurnFence(max_closed=cap)
    ids = [f"t{i}" for i in range(cap + 1)]  # one more than the cap
    for tid in ids:
        f.open(tid)
        f.close(tid)
        assert len(f._closed) <= cap  # closed set never grows past the cap

    oldest = ids[0]
    assert oldest not in f._closed  # oldest closed id was evicted
    assert ids[-1] in f._closed     # a recently-closed id is still retained
    # observable behavior for the evicted id is still "discard" (unknown turn)
    assert f.accept(_result_env(oldest)) is False
