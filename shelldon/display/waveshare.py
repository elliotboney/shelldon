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
#: The panel is carved into three stacked zones (B.3): a thin battery strip on top, a thin
#: caption strip on the bottom, and the face filling the band between. Heights tuned so the
#: face keeps most of the panel while both status lines stay legible at Unifont's bitmap sizes.
_BATTERY_H = 18
_CAPTION_H = 22

#: The regions this renderer composites onto the one physical panel, top→bottom. A snapshot
#: for any OTHER region (e.g. the XP `STATUS_BAR` widget) is ignored — one panel, three zones.
_ZONES = (Region.FACE, Region.BATTERY, Region.CAPTION)


def _layout(w: int = _CANVAS_W, h: int = _CANVAS_H) -> dict[Region, tuple[int, int, int, int]]:
    """Pure zone geometry: stacked, non-overlapping (x, y, w, h) boxes per composited region —
    battery top strip, caption bottom strip, face the band between. Pure (no PIL) so the layout
    invariant (no overlap, within canvas) is testable on a laptop where pillow isn't installed."""
    return {
        Region.BATTERY: (0, 0, w, _BATTERY_H),
        Region.FACE: (0, _BATTERY_H, w, h - _BATTERY_H - _CAPTION_H),
        Region.CAPTION: (0, h - _CAPTION_H, w, _CAPTION_H),
    }


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
        #: Latest text per composited region — the renderer is STATEFUL because the E-Ink
        #: panel is a single framebuffer: every `epd.display()` repaints the WHOLE panel, so
        #: drawing one zone alone would erase the others. Each snapshot updates one slot, then
        #: the full canvas recomposites. FACE holds a token (mapped via `face_for`);
        #: BATTERY/CAPTION hold literal widget text.
        self._zones: dict[Region, str] = {}

    async def render(self, snapshot: StateSnapshot) -> None:
        # Three zones share the one panel (B.3). A snapshot for any other region (the XP
        # STATUS_BAR widget) is ignored rather than fighting the face for the framebuffer.
        if snapshot.region not in _ZONES:
            log.debug("waveshare: ignoring non-composited region %s", snapshot.region)
            return
        self._zones[snapshot.region] = snapshot.face
        await asyncio.to_thread(self._draw_blocking)

    # --- everything below is hardware/PIL-gated (runs only on the Pi) ---

    def _draw_blocking(self) -> None:  # pragma: no cover - real E-Ink on the Pi
        try:
            epd = self._ensure_panel()
            image = self._render_image()
            epd.display(epd.getbuffer(image))
        except Exception as exc:
            # A display failure must NEVER take down the soul (AD-13) — log + skip the frame.
            log.warning("waveshare draw failed (%s); skipping frame", exc)

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

    def _render_image(self):  # pragma: no cover - needs pillow (Pi component dep)
        """Composite every known zone onto one white canvas (the full-panel repaint). A zone
        with no snapshot yet (e.g. no battery on a panel without the HAT) simply isn't drawn."""
        from PIL import Image, ImageDraw

        img = Image.new("1", (_CANVAS_W, _CANVAS_H), 1)  # 1 = white
        draw = ImageDraw.Draw(img)
        boxes = _layout()
        if Region.FACE in self._zones:  # the mood expression — big, centered in its band
            self._draw_centered(draw, face_for(self._zones[Region.FACE]), boxes[Region.FACE], start=64)
        if Region.BATTERY in self._zones:  # small, right-aligned in the top strip
            self._draw_aligned(draw, self._zones[Region.BATTERY], boxes[Region.BATTERY], size=14)
        if Region.CAPTION in self._zones:  # small, centered + shrunk-to-fit in the bottom strip
            self._draw_centered(draw, self._zones[Region.CAPTION], boxes[Region.CAPTION], start=18)
        return img

    def _draw_centered(self, draw, text: str, box, start: int):  # pragma: no cover - needs pillow
        """Fit `text` to the box (shrinking the font) and center it within the box."""
        bx, by, bw, bh = box
        font, (tw, th, lx, ty) = self._fit(draw, text, bw, bh, start)
        draw.text((bx + (bw - tw) // 2 - lx, by + (bh - th) // 2 - ty), text, font=font, fill=0)

    def _draw_aligned(self, draw, text: str, box, size: int):  # pragma: no cover - needs pillow
        """Draw `text` at a FIXED size, right-aligned + vertically centered in the box (the
        battery widget: a stable small size reads better than one that resizes every poll)."""
        bx, by, bw, bh = box
        font = self._font(size)
        lx, ty, rx, by2 = draw.textbbox((0, 0), text, font=font)
        tw, th = rx - lx, by2 - ty
        draw.text((bx + bw - tw - lx - _MARGIN, by + (bh - th) // 2 - ty), text, font=font, fill=0)

    def _fit(self, draw, text: str, max_w: int, max_h: int, start: int = 64):  # pragma: no cover
        """Largest font size whose rendered text fits the given box (minus a small margin)."""
        mw, mh = max_w - 2 * _MARGIN, max_h - 2
        for size in range(start, 8, -2):
            font = self._font(size)
            lx, ty, rx, by = draw.textbbox((0, 0), text, font=font)
            if (rx - lx) <= mw and (by - ty) <= mh:
                return font, (rx - lx, by - ty, lx, ty)
        font = self._font(10)
        lx, ty, rx, by = draw.textbbox((0, 0), text, font=font)
        return font, (rx - lx, by - ty, lx, ty)
