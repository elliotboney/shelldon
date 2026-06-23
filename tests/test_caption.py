"""B.3 — the bottom caption strip: core pushes a short 'what I'm doing/feeling/just said'
line to Region.CAPTION alongside the face, so the desk pet reacts in text (the v1 feel).

`_caption_for` is pure (the reply→line truncation); the push path is checked at the core
level against the same bus spy the faces tests use."""

from conftest import DummySpawner
from shelldon.contracts import Actor, Envelope, MsgKind, Region, Result
from shelldon.core.runtime import _CAPTION_MAX, Core, _caption_for


def _core_for_result(sock_path, tmp_path):
    core = Core(
        sock_path,
        DummySpawner(),
        memory_root=tmp_path / "memory",
        history_path=tmp_path / "history.db",
        checkpoint_path=tmp_path / "state.json",
        faces_path=tmp_path / "faces.toml",
    )
    core.arbiter.submit("hi")  # mark a turn in flight so complete() balances
    core._current_prompt, core._current_turn_id = "hi", "t1"
    core.fence.open("t1")
    return core


def _reply_env(*, payload, blurb):
    return Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=True, payload=payload, blurb=blurb), turn_id="t1",
    )


def test_caption_for_uses_first_line_trimmed():
    assert _caption_for("on it — reminding you at 5pm 🐢") == "on it — reminding you at 5pm 🐢"
    assert _caption_for("  hello  \n second line") == "hello"  # first line, trimmed


def test_caption_for_empty_is_a_resting_ellipsis():
    # A dream that only proposed ops (empty payload) still shows the pet is alive.
    assert _caption_for("") == "…"
    assert _caption_for("   \n  ") == "…"


def test_caption_for_truncates_long_text_with_ellipsis():
    long = "x" * 200
    out = _caption_for(long)
    assert len(out) == _CAPTION_MAX
    assert out.endswith("…")


def _captions(core) -> list[str]:
    """Spy on core's bus, recording only CAPTION-region snapshots."""
    seen: list[str] = []

    async def fake_deliver(env):
        if env.kind is MsgKind.STATE_SNAPSHOT and env.body.region is Region.CAPTION:
            seen.append(env.body.face)

    core.bus.deliver = fake_deliver
    return seen


async def test_idle_mood_drift_pushes_a_caption(sock_path, tmp_path):
    # Between turns the caption settles to the mood word (it rides the same mood-face push).
    core = Core(
        sock_path,
        DummySpawner(),
        checkpoint_path=tmp_path / "state.json",
        faces_path=tmp_path / "faces.toml",
    )
    captions = _captions(core)
    core.state.apply_patch({"mood.valence": -0.6, "energy": 0.6})  # → grumpy
    await core._maybe_push_mood_face()
    assert captions == ["grumpy"]
    assert core._last_caption == "grumpy"


async def test_reply_caption_lingers_then_settles_to_mood(sock_path, tmp_path):
    # A reply's real text must LINGER (not flash) — the mood word may only replace it once
    # the dwell has elapsed (B.3 review). Drive an injected monotonic clock past the dwell.
    clock = [0.0]
    core = Core(
        sock_path,
        DummySpawner(),
        checkpoint_path=tmp_path / "state.json",
        faces_path=tmp_path / "faces.toml",
        monotonic=lambda: clock[0],
    )
    captions = _captions(core)
    core.state.apply_patch({"mood.valence": -0.6, "energy": 0.6})  # mood would drift → grumpy

    await core._push_caption("reminding you at 5pm", dwell=60.0)  # a reply lands at t=0
    assert captions == ["reminding you at 5pm"]

    clock[0] = 10.0  # a reflex tick mid-dwell must NOT overwrite the reply
    await core._maybe_push_mood_face()
    assert core._last_caption == "reminding you at 5pm"
    assert "grumpy" not in captions

    clock[0] = 61.0  # past the dwell — now it settles to the mood word
    await core._maybe_push_mood_face()
    assert core._last_caption == "grumpy"
    assert captions == ["reminding you at 5pm", "grumpy"]


async def test_handle_result_caption_prefers_the_models_thought(sock_path, tmp_path):
    # The model's distilled THOUGHT (B.3 / v1) wins over a truncation of the spoken reply.
    core = _core_for_result(sock_path, tmp_path)
    captions = _captions(core)
    await core._handle_result(_reply_env(payload="Sure, reminding you at 5pm!", blurb="happy to help"))
    assert captions == ["happy to help"]


async def test_handle_result_caption_falls_back_to_reply_without_thought(sock_path, tmp_path):
    # No THOUGHT line → the screen falls back to a truncation of the reply (graceful).
    core = _core_for_result(sock_path, tmp_path)
    captions = _captions(core)
    await core._handle_result(_reply_env(payload="Hello there, friend", blurb=""))
    assert captions == ["Hello there, friend"]
