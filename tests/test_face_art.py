"""Story 8.3 — the E-Ink face-art map (pure; the panel/PIL bits are Pi-gated).

The renderer's hardware path (`spidev`/`gpiozero`/`pillow`/the Waveshare driver) is
`# pragma: no cover` — exercised on the Pi. What IS testable off-Pi: every face token the
pet can emit has an expression to draw, the fallback is sane, and the module imports without
the hardware deps (so `app.py` can reference it on a laptop)."""

from shelldon.contracts import Region, StateSnapshot
from shelldon.core.faces import DEFAULT_FACES
from shelldon.core.runtime import FACE_DEGRADED, FACE_REPLY, FACE_THINKING
from shelldon.display.waveshare import (
    _CANVAS_H,
    _CANVAS_W,
    _ZONES,
    FACE_ART,
    WaveshareRenderer,
    _layout,
    face_for,
)


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


# --- B.3: 3-zone compositing (pure layout + stateful zone accumulation; PIL draw is Pi-gated) ---


def test_layout_zones_stack_without_overlap_and_fit_canvas():
    boxes = _layout()
    assert set(boxes) == {Region.BATTERY, Region.FACE, Region.CAPTION}
    # Each box is within the canvas...
    for x, y, w, h in boxes.values():
        assert x >= 0 and y >= 0 and x + w <= _CANVAS_W and y + h <= _CANVAS_H and h > 0
    # ...and the three stack top→bottom with no vertical overlap (battery, face, caption).
    bx = boxes[Region.BATTERY]
    fx = boxes[Region.FACE]
    cx = boxes[Region.CAPTION]
    assert bx[1] + bx[3] == fx[1]  # battery bottom == face top
    assert fx[1] + fx[3] == cx[1]  # face bottom == caption top
    assert cx[1] + cx[3] == _CANVAS_H  # caption sits flush to the bottom edge


async def test_render_accumulates_zones_and_ignores_non_composited(monkeypatch):
    # The panel is one framebuffer, so the renderer is stateful: each snapshot updates ONE
    # zone slot (the full redraw is Pi-gated, so stub it out here). A region it doesn't
    # composite (the XP STATUS_BAR widget) is ignored, never stored.
    r = WaveshareRenderer()
    monkeypatch.setattr(r, "_draw_blocking", lambda: None)  # skip the PIL/E-Ink path off-Pi

    async def push(region, text):
        await r.render(StateSnapshot(region=region, seq=1, face=text))

    await push(Region.FACE, "happy")
    await push(Region.BATTERY, "87%")
    await push(Region.CAPTION, "on it!")
    await push(Region.STATUS_BAR, "Lv2 · 120 XP")  # not a composited zone → ignored

    assert r._zones == {Region.FACE: "happy", Region.BATTERY: "87%", Region.CAPTION: "on it!"}
    assert Region.STATUS_BAR not in r._zones
    assert Region.STATUS_BAR not in _ZONES
