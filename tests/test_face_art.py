"""Story 8.3 — the E-Ink face-art map (pure; the panel/PIL bits are Pi-gated).

The renderer's hardware path (`spidev`/`gpiozero`/`pillow`/the Waveshare driver) is
`# pragma: no cover` — exercised on the Pi. What IS testable off-Pi: every face token the
pet can emit has an expression to draw, the fallback is sane, and the module imports without
the hardware deps (so `app.py` can reference it on a laptop)."""

from shelldon.core.faces import DEFAULT_FACES
from shelldon.core.runtime import FACE_DEGRADED, FACE_REPLY, FACE_THINKING
from shelldon.display.waveshare import FACE_ART, WaveshareRenderer, face_for


def test_every_starter_face_token_has_art():
    # The 6 starter faces (core/faces.py) — each name is a token core pushes via select().
    for face in DEFAULT_FACES:
        token = face.token or face.name
        assert token in FACE_ART, f"no E-Ink art for starter face {token!r}"


def test_every_lifecycle_token_has_art():
    # The 3 lifecycle tokens core pushes directly (runtime.py): thinking / reply / degraded.
    for token in (FACE_THINKING, FACE_REPLY, FACE_DEGRADED):
        assert token in FACE_ART, f"no E-Ink art for lifecycle face {token!r}"


def test_face_for_returns_mapped_art():
    assert face_for("happy") == FACE_ART["happy"]
    assert face_for("content") == FACE_ART["content"]


def test_face_for_unknown_token_falls_back_to_the_token_text():
    # A self-added face (Story 3.4) with no art still shows something legible, not a blank.
    assert face_for("sparkly-new-mood") == "sparkly-new-mood"


def test_renderer_constructs_without_hardware_deps():
    # Importing + constructing must NOT require pillow/spidev/the driver (all lazy on draw),
    # so app.py can reference WaveshareRenderer on a laptop and only the Pi touches hardware.
    r = WaveshareRenderer()
    assert r._epd is None  # panel not initialised until the first render
