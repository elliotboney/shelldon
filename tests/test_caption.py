"""B.3 — the bottom caption strip: core pushes a short 'what I'm doing/feeling/just said'
line to Region.CAPTION alongside the face, so the desk pet reacts in text (the v1 feel).

`_caption_for` is pure (the reply→line truncation); the push path is checked at the core
level against the same bus spy the faces tests use."""

from conftest import DummySpawner
from shelldon.contracts import MsgKind, Region
from shelldon.core.runtime import _CAPTION_MAX, Core, _caption_for


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
