"""Story 3.3 — the self-modifiable faces registry + mood→face mapping.

Covers the editable TOML registry (seed on first run, corruption-tolerant load —
AD-7/3.1 discipline), the pure `select_face` mapping (AC2), and the validated,
atomic, comment-preserving `add_face` apply path (AC3 — the "core applies" half;
the chat-driven proposal is Story 3.4).

Every test injects a `tmp_path` faces file — never real `$HOME`.
"""

import os

import pytest

from conftest import DummySpawner
from shelldon.contracts import MsgKind, Region
from shelldon.core.faces import (
    DEFAULT_FACES,
    STARTER_NAMES,
    Face,
    FaceRegistry,
    select_face,
)
from shelldon.core.runtime import Core


def _record_pushes(core) -> list[str]:
    """Spy on core's bus so FACE pushes are observable without a running bus
    (an unregistered DISPLAY just drops the frame). Returns the recorded tokens.
    Filters to the FACE region — the bottom CAPTION strip (B.3) is a separate
    region stream and would otherwise double-count a mood push (same token)."""
    pushed: list[str] = []

    async def fake_deliver(env):
        if env.kind is MsgKind.STATE_SNAPSHOT and env.body.region is Region.FACE:
            pushed.append(env.body.face)

    core.bus.deliver = fake_deliver
    return pushed


def _core(sock_path, tmp_path):
    return Core(
        sock_path,
        DummySpawner(),
        checkpoint_path=tmp_path / "state.json",
        faces_path=tmp_path / "faces.toml",
    )


# --- AC1: registry load — seed on first run, corruption-tolerant ---


def test_absent_file_seeds_the_starter_set(tmp_path):
    path = tmp_path / "faces.toml"
    reg = FaceRegistry.load(path)

    assert path.exists()  # seeded so the owner has something to edit
    assert {f.name for f in reg.faces} == STARTER_NAMES
    assert len(STARTER_NAMES) == 6


def test_valid_file_is_loaded(tmp_path):
    path = tmp_path / "faces.toml"
    FaceRegistry.load(path)  # seed
    reg = FaceRegistry.load(path)  # re-load the seeded file
    assert {f.name for f in reg.faces} == STARTER_NAMES


def test_corrupt_toml_falls_back_to_defaults(tmp_path):
    path = tmp_path / "faces.toml"
    path.write_text("this is not [ valid toml = =")
    reg = FaceRegistry.load(path)
    assert {f.name for f in reg.faces} == STARTER_NAMES  # built-in fallback, no raise


def test_invalid_entry_falls_back_to_defaults(tmp_path):
    path = tmp_path / "faces.toml"
    # Well-formed TOML, but a face with an inverted range (lo > hi) is invalid.
    path.write_text(
        '[[face]]\nname = "broken"\nvalence = [0.5, -0.5]\n'
        "arousal = [0.0, 1.0]\nenergy = [0.0, 1.0]\n"
    )
    reg = FaceRegistry.load(path)
    assert {f.name for f in reg.faces} == STARTER_NAMES


def test_out_of_range_entry_falls_back_to_defaults(tmp_path):
    path = tmp_path / "faces.toml"
    path.write_text(
        '[[face]]\nname = "toohot"\nvalence = [0.0, 2.0]\n'  # 2.0 > valence max 1.0
        "arousal = [0.0, 1.0]\nenergy = [0.0, 1.0]\n"
    )
    reg = FaceRegistry.load(path)
    assert {f.name for f in reg.faces} == STARTER_NAMES


# --- AC2: pure select_face mapping ---


def test_select_each_starter_emotion_is_reachable():
    """Every starter face must be selectable by some mood — else it's dead."""
    reg = FaceRegistry(list(DEFAULT_FACES), None)
    moods = {
        "low-battery": (0.0, 0.0, 0.05),
        "sleepy": (0.0, -0.6, 0.3),
        "grumpy": (-0.6, 0.0, 0.6),
        "excited": (0.8, 0.7, 0.8),
        "curious": (0.2, 0.4, 0.5),
        "content": (0.1, 0.0, 0.6),
    }
    for expected, (v, a, e) in moods.items():
        assert reg.select(v, a, e) == expected, f"{(v, a, e)} should map to {expected}"


def test_select_is_deterministic_and_pure():
    faces = list(DEFAULT_FACES)
    reg = FaceRegistry(faces, None)
    t1 = reg.select(0.8, 0.7, 0.8)
    t2 = reg.select(0.8, 0.7, 0.8)
    assert t1 == t2
    assert reg.faces == faces  # selection mutates nothing


def test_select_falls_back_to_default_when_nothing_matches():
    # A registry whose only face matches nothing -> the defined default token.
    lonely = [Face(name="never", valence=(0.99, 1.0), arousal=(0.99, 1.0), energy=(0.99, 1.0))]
    reg = FaceRegistry(lonely, None)
    assert reg.select(0.0, 0.0, 0.5) == "content"  # the defined DEFAULT_FACE_TOKEN


def test_starter_tokens_are_distinct():
    tokens = [f.token or f.name for f in DEFAULT_FACES]
    assert len(set(tokens)) == len(tokens) == 6  # AC3: six visibly distinct tokens


# --- AC3: validated, atomic, comment-preserving add_face ---


def test_add_face_persists_and_is_selectable(tmp_path):
    path = tmp_path / "faces.toml"
    reg = FaceRegistry.load(path)
    reg.add_face("smug", valence=(0.3, 1.0), arousal=(-0.2, 0.2), energy=(0.4, 1.0))

    assert any(f.name == "smug" for f in reg.faces)  # in RAM
    reloaded = FaceRegistry.load(path)  # and on disk
    assert any(f.name == "smug" for f in reloaded.faces)


def test_add_face_preserves_human_comments(tmp_path):
    path = tmp_path / "faces.toml"
    reg = FaceRegistry.load(path)
    # The seed writes a header comment; a hand-added comment must also survive a write.
    text = path.read_text()
    assert text.lstrip().startswith("#")  # seeded header comment exists
    header_line = text.splitlines()[0]

    reg.add_face("smug", valence=(0.3, 1.0), arousal=(-0.2, 0.2), energy=(0.4, 1.0))
    after = path.read_text()
    assert header_line in after  # tomlkit preserved the comment on rewrite


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "valence": (0.0, 1.0), "arousal": (0.0, 1.0), "energy": (0.0, 1.0)},  # empty name
        {"name": "bad", "valence": (0.5, -0.5), "arousal": (0.0, 1.0), "energy": (0.0, 1.0)},  # lo>hi
        {"name": "bad", "valence": (-2.0, 1.0), "arousal": (0.0, 1.0), "energy": (0.0, 1.0)},  # out of range
        {"name": "content", "valence": (0.0, 1.0), "arousal": (0.0, 1.0), "energy": (0.0, 1.0)},  # dup (no replace)
    ],
)
def test_add_face_rejects_invalid_and_leaves_everything_unchanged(tmp_path, kwargs):
    path = tmp_path / "faces.toml"
    reg = FaceRegistry.load(path)
    before_names = {f.name for f in reg.faces}
    before_text = path.read_text()

    with pytest.raises(ValueError):
        reg.add_face(**kwargs)

    assert {f.name for f in reg.faces} == before_names  # RAM unchanged
    assert path.read_text() == before_text  # file unchanged


def test_add_face_replace_allows_overwriting_an_existing_name(tmp_path):
    path = tmp_path / "faces.toml"
    reg = FaceRegistry.load(path)
    reg.add_face("content", valence=(0.0, 0.1), arousal=(0.0, 0.1), energy=(0.9, 1.0), replace=True)
    contents = [f for f in reg.faces if f.name == "content"]
    assert len(contents) == 1  # replaced, not duplicated


def test_add_face_atomic_write_leaves_prior_file_on_crash(tmp_path, monkeypatch):
    """AD-10: a write interrupted before os.replace leaves the prior file intact."""
    path = tmp_path / "faces.toml"
    reg = FaceRegistry.load(path)
    good = path.read_text()

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        reg.add_face("smug", valence=(0.3, 1.0), arousal=(-0.2, 0.2), energy=(0.4, 1.0))

    assert path.read_text() == good  # prior registry intact
    assert list(tmp_path.iterdir()) == [path]  # no stray temp file


# --- AC2: core pushes the mood face between turns (gated on no turn in flight) ---


async def test_mood_face_pushed_when_idle(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    pushed = _record_pushes(core)
    core.state.apply_patch({"mood.valence": -0.6, "energy": 0.6})  # -> grumpy

    await core._maybe_push_mood_face()

    assert pushed == ["grumpy"]
    assert core._last_face == "grumpy"


async def test_no_mood_push_during_a_turn(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    pushed = _record_pushes(core)
    core.arbiter.worker_in_flight = True  # a turn owns the screen
    core.state.apply_patch({"mood.valence": -0.6, "energy": 0.6})

    await core._maybe_push_mood_face()

    assert pushed == []  # lifecycle face stands; no mood push mid-turn


async def test_mood_push_suppressed_when_unchanged(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    pushed = _record_pushes(core)
    core.state.apply_patch({"mood.valence": -0.6, "energy": 0.6})

    await core._maybe_push_mood_face()
    await core._maybe_push_mood_face()  # same token -> no second push

    assert pushed == ["grumpy"]


async def test_mood_face_restored_after_a_lifecycle_face(sock_path, tmp_path):
    """A lifecycle push (e.g. reply 'happy') updates _last_face; the next idle tick
    restores the mood-derived face since it differs."""
    core = _core(sock_path, tmp_path)
    pushed = _record_pushes(core)

    await core._push_face("happy")  # lifecycle reply face
    assert core._last_face == "happy"

    await core._maybe_push_mood_face()  # default mood -> content, differs from 'happy'
    assert pushed[-1] == "content"


def test_core_apply_add_face_validates_and_persists(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    core.apply_add_face("smug", valence=(0.3, 1.0), arousal=(-0.2, 0.2), energy=(0.4, 1.0))

    assert any(f.name == "smug" for f in core.faces.faces)
    reloaded = FaceRegistry.load(core.faces.path)
    assert any(f.name == "smug" for f in reloaded.faces)

    with pytest.raises(ValueError):
        core.apply_add_face("oops", valence=(1.0, -1.0), arousal=(0.0, 1.0), energy=(0.0, 1.0))
