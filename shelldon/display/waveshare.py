"""The real Waveshare 2.13" V4 E-Ink renderer (Story 8.3) — the production `Renderer`.

"Ship the mechanism + seam, gate the hardware" (the StubRenderer / fork-uid precedent):
the panel needs `spidev` + `gpiozero` + `pillow` + the vendored Waveshare driver
(`drivers/epd2in13_V4.py`), all Linux/Pi-only — so they are **lazily imported inside the
methods**, never at module load. This module imports cleanly on a laptop (no PIL/spidev),
so `app.py` can reference it unconditionally; only `start()`/`render()` touch the hardware,
and those run only on the Pi. The deps are component-local install-time deps (the Pi does
`uv pip install pillow spidev gpiozero lgpio`), NOT spine deps — `uv sync --locked` stays
0-new-deps, exactly like `bleak`/PiSugar in Story 7.4.

The pet's faces are MOOD TOKENS (`content`, `excited`, …, plus the lifecycle `thinking`/
`happy`/`cant-think`); `FACE_ART` maps each to a chunky Unicode expression rendered with
GNU Unifont (full BMP coverage → no tofu, and the bitmap look suits E-Ink). An unknown token
renders as its own text, so a self-modified face (Story 3.4) still shows *something*.
"""

import asyncio
import logging
import os

from shelldon.contracts import Region, StateSnapshot

log = logging.getLogger("shelldon.display.waveshare")

#: token -> the expression drawn on the panel. The 6 starter faces (core/faces.py) + the 3
#: lifecycle tokens (core/runtime.py). Owner-tuned 2026-06-20.
FACE_ART: dict[str, str] = {
    "content": "(•‿•)",
    "happy": "(◠‿◠✿)",
    "excited": "٩(⚙ᴗ⚙)۶",
    "curious": "٩(๏̯๏)۶",
    "thinking": "Σ(-᷅_-᷄ ๑)",
    "sleepy": "(_ _ ) Zzz z",
    "grumpy": "(>_<)",
    "cant-think": "(⊙_◎)",
    "low-battery": "(u_u)",
}

#: GNU Unifont — full Basic-Multilingual-Plane coverage so every glyph in FACE_ART renders
#: (no missing-glyph tofu). Overridable for a different panel/box via env.
DEFAULT_FONT_PATH = os.environ.get(
    "SHELLDON_FACE_FONT", "/usr/share/fonts/opentype/unifont/unifont.otf"
)

#: The panel is 122x250 portrait; the driver's getbuffer() rotates a landscape image, so we
#: draw faces wide (landscape) — they read better that way.
_CANVAS_W, _CANVAS_H = 250, 122
_MARGIN = 7


def face_for(token: str) -> str:
    """The expression to draw for a face token — the mapped art, or the token text itself
    as a fallback so an unmapped/self-added face still shows something legible. Pure."""
    return FACE_ART.get(token, token)


class WaveshareRenderer:
    """A `Renderer` that draws the pet's face token onto the real 2.13" V4 panel. Lazily
    initialises the panel on the first render (the `init()` is slow), and runs each draw in a
    worker thread (`asyncio.to_thread`) — the E-Ink refresh is ~2s blocking I/O the event loop
    must not stall on (NFR3). The display service renders serially, so there's never a
    concurrent draw on the single panel object."""

    def __init__(self, *, font_path: str = DEFAULT_FONT_PATH) -> None:
        self._font_path = font_path
        self._epd = None  # the panel, lazily created on first render (Pi-only)
        self._font_cache: dict[int, object] = {}

    async def render(self, snapshot: StateSnapshot) -> None:
        # One physical panel = the soul's FACE region. Plugin widget regions (STATUS_BAR)
        # would need on-panel compositing — a follow-on; ignore them here rather than letting
        # an XP widget overwrite the face.
        if snapshot.region is not Region.FACE:
            log.debug("waveshare: ignoring non-FACE region %s", snapshot.region)
            return
        await asyncio.to_thread(self._draw_blocking, snapshot.face)

    # --- everything below is hardware/PIL-gated (runs only on the Pi) ---

    def _draw_blocking(self, token: str) -> None:  # pragma: no cover - real E-Ink on the Pi
        try:
            epd = self._ensure_panel()
            image = self._render_image(token)
            epd.display(epd.getbuffer(image))
        except Exception as exc:
            # A display failure must NEVER take down the soul (AD-13) — log + skip the frame.
            log.warning("waveshare draw failed for %r (%s); skipping frame", token, exc)

    def _ensure_panel(self):  # pragma: no cover - real E-Ink on the Pi
        if self._epd is None:
            from shelldon.display.drivers.epd2in13_V4 import EPD

            epd = EPD()
            epd.init()
            epd.Clear(0xFF)
            self._epd = epd
            log.info("waveshare panel initialised (%dx%d)", epd.width, epd.height)
        return self._epd

    def _font(self, size: int):  # pragma: no cover - needs pillow (Pi component dep)
        from PIL import ImageFont

        if size not in self._font_cache:
            self._font_cache[size] = ImageFont.truetype(self._font_path, size)
        return self._font_cache[size]

    def _render_image(self, token: str):  # pragma: no cover - needs pillow (Pi component dep)
        from PIL import Image, ImageDraw

        text = face_for(token)
        img = Image.new("1", (_CANVAS_W, _CANVAS_H), 1)  # 1 = white
        draw = ImageDraw.Draw(img)
        font, (tw, th, lx, ty) = self._fit(draw, text)
        draw.text(((_CANVAS_W - tw) // 2 - lx, (_CANVAS_H - th) // 2 - ty), text, font=font, fill=0)
        return img

    def _fit(self, draw, text: str, start: int = 64):  # pragma: no cover - needs pillow
        """Largest font size whose rendered text fits the canvas (minus margin), centered."""
        max_w, max_h = _CANVAS_W - 2 * _MARGIN, _CANVAS_H - 2 * _MARGIN
        for size in range(start, 12, -2):
            font = self._font(size)
            lx, ty, rx, by = draw.textbbox((0, 0), text, font=font)
            if (rx - lx) <= max_w and (by - ty) <= max_h:
                return font, (rx - lx, by - ty, lx, ty)
        font = self._font(14)
        lx, ty, rx, by = draw.textbbox((0, 0), text, font=font)
        return font, (rx - lx, by - ty, lx, ty)
