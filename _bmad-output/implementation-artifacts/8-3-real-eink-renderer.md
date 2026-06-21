---
baseline_commit: 85be6ff
---
# Story 8.3: The face on the screen — real Waveshare E-Ink renderer

Status: done

<!-- Epic 8 (Verify & Deploy), story 4 (8.2 = real chat transport is the still-open follow-on from 8.1). Built locally (suite-green) then verified ON THE REAL PANEL on `gotchi`. Closes the panel-emulator-render-spike icebox ("prove ONE render path for the 6 faces"). v1 left stopped (we used its display hardware). -->

## Story

As the owner,
I want the pet's face to actually show on the Waveshare E-Ink panel — its mood and lifecycle expressions rendered on the glass as it thinks, replies, and drifts,
so that shelldon is a thing on my desk with a face, not just a headless process — the embodiment payoff, on the real hardware.

**Why now:** Story 8.1 proved shelldon runs headless on the Pi (fork/OOM model + live GLM). The last missing piece of "a pet on the desk" is the face. The render path was the longest-standing gated stub (`StubRenderer` since Story 1.7; the `panel-emulator-render-spike` icebox).

## Acceptance Criteria (proven on hardware)

### AC1 — A real `Renderer` draws shelldon's face tokens to the 2.13" V4 panel ✅

**Given** the `Renderer` seam (Story 1.7: `async render(snapshot)`) and the panel = Waveshare 2.13" V4, 122×250 mono
**When** `WaveshareRenderer` is built
**Then** it maps each face TOKEN to a chunky Unicode expression (`FACE_ART`) and draws it centered, auto-sized, with **GNU Unifont** (full BMP coverage → the flower/gears/Thai/combining-diacritics all render, no tofu; bitmap look suits E-Ink). The vendored Waveshare driver (`drivers/epd2in13_V4.py` + `epdconfig.py`, MIT, lifted from v1's working driver), `pillow`, `spidev`, `gpiozero`+`lgpio` are **lazily imported inside the methods** (component-local install-time deps — the Pi does `uv pip install pillow spidev gpiozero lgpio rpi-lgpio` + `apt install liblgpio-dev swig`); the module imports cleanly on a laptop. The slow ~2s E-Ink refresh runs in `asyncio.to_thread` (NFR3).

### AC2 — A live GLM turn drives the face on the panel, end-to-end ✅

**Given** the app on the Pi with `SHELLDON_DISPLAY=waveshare` (`_default_renderer` picks the real panel; else `StubRenderer`)
**When** a real owner turn runs against live GLM-4.7
**Then** the panel showed the full lifecycle live: `thinking` (`Σ(-᷅_-᷄ ๑)`) during the turn → `happy` (`(◠‿◠✿)`) on the reply → the **mood face** (`curious` `٩(๏̯๏)۶`) as the pet settled between turns (Story 3.3's mood→face compositor, confirmed on glass). Log: `waveshare panel initialised (122x250)`; reply returned ("Beep boop! I'm running smoothly…"); no errors. The mood expression persists (E-Ink holds the last image without power).

### AC3 — The boundary holds (0 spine deps, gated, suite green) ✅

**Given** the "ship the seam, gate the hardware" discipline (`bleak`/PiSugar precedent, 7.4)
**Then** `uv sync --locked` **0 new deps** (pillow/spidev/gpiozero/lgpio are Pi-only, lazy-imported, never in `pyproject`); `uv run lint-imports` **3 contracts KEPT**; the laptop suite is **542 green** (+5: a pure `FACE_ART` coverage test — every emittable token has art, unknown→token-text fallback, the renderer constructs without the hardware deps). `core/` byte-unchanged; the only wiring is `app.py`'s `_default_renderer` gate.

### Out of scope (explicit — follow-ons)

- **`epd.sleep()` on teardown** — the renderer holds the last face (fine for a desk pet; E-Ink persists) but doesn't sleep the panel on shutdown. Tidy follow-on (logged).
- **STATUS_BAR (plugin widget) on-panel compositing** — `WaveshareRenderer` renders only the FACE region; an XP/sensor widget sharing the one panel needs a compositor. The renderer ignores non-FACE regions for now (doesn't let a widget overwrite the face).
- **Partial-refresh / animation** — full refresh per face (~2s); partial-refresh for blinks/idle is a later polish (the driver supports `displayPartial`).
- **Real chat transport (Story 8.2)** + sensors + systemd service — separate Epic 8 stories.

## What was done (run book, on `gotchi`)

1. Vendored v1's working driver → `shelldon/display/drivers/{epd2in13_V4,epdconfig}.py` (fixed `import epdconfig` → `from . import epdconfig`).
2. Built `shelldon/display/waveshare.py` (`FACE_ART` + `WaveshareRenderer`) + `app.py` `_default_renderer(env)` gate.
3. Confirmed font coverage off-panel first: rendered the 9 faces to a PNG with Unifont/Symbola/DejaVu, pulled it back, eyeballed it — **Unifont renders every glyph** (the others tofu'd some). Owner tuned the faces.
4. Drove the real panel with a standalone script (init → cycle happy/thinking/content → sleep) — owner confirmed "saw them all flash, looks great".
5. Committed (`af3e1a3`); on the Pi: `git pull`, `apt install swig liblgpio-dev`, `uv pip install pillow spidev gpiozero lgpio rpi-lgpio` into shelldon's venv.
6. Ran a live GLM turn with `SHELLDON_DISPLAY=waveshare GPIOZERO_PIN_FACTORY=lgpio` → reply returned + the face cycled `thinking → happy → curious` on the panel (owner confirmed).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Completion Notes List

- **The pet has a face on the desk.** A live GLM turn on the 416MB Pi drove the real 2.13" V4 panel through the full expression lifecycle (`thinking → happy → mood`), proving not just the lifecycle faces but Story 3.3's live mood→face compositor on glass. Combined with 8.0 (live brain) + 8.1 (fork/OOM on the Pi), the whole stack — memory, autonomy, live LLM, embodied face — now runs on the target hardware.
- **Font was the real risk, retired early.** The owner-tuned faces use rich Unicode (✿⚙๏ + combining diacritics ◎); a render-to-PNG-and-look-at-it loop (off-panel) proved **GNU Unifont** renders every glyph before any hardware was touched. That PNG preview loop is a reusable trick for E-Ink work.
- **Deps stayed off the spine.** pillow/spidev/gpiozero/lgpio are component-local (Pi `uv pip install` + `apt liblgpio-dev swig` for the lgpio build) and lazy-imported — `uv sync --locked` 0-new-deps, the module imports on a laptop, the suite stays green (542, +5 pure tests). Same discipline as 7.4's bleak/PiSugar.
- **Install gotcha worth recording:** the Pi's `lgpio` Python pkg builds from source and needs `swig` + `liblgpio-dev` (apt) to compile/link; without them the whole `uv pip install` batch aborts (install the wheel deps separately first). Captured in the run book.
- **Two tidy follow-ons** (deferred-work.md): `epd.sleep()` on teardown; STATUS_BAR on-panel compositing.

### File List

- `shelldon/display/waveshare.py` — NEW. `FACE_ART` map + `WaveshareRenderer` (lazy hardware/PIL, `asyncio.to_thread` draw).
- `shelldon/display/drivers/epd2in13_V4.py`, `epdconfig.py`, `__init__.py` — NEW (vendored Waveshare driver, MIT; relative-import fix).
- `shelldon/app.py` — MODIFIED. `_default_renderer(env)` gate (SHELLDON_DISPLAY); the display child + run_app use it.
- `tests/test_face_art.py` — NEW. 5 pure coverage/fallback/clean-import tests.
- `_bmad-output/implementation-artifacts/{sprint-status.yaml,deferred-work.md,8-3-real-eink-renderer.md}` — tracking.

### Change Log

- 2026-06-20 — Story 8.3 done (built locally suite-green, then verified ON THE PANEL on `gotchi`): real Waveshare 2.13" V4 E-Ink renderer behind the StubRenderer seam. A live GLM turn drove the face through `thinking → happy → curious` (mood) on the glass — embodiment proven on the 416MB Pi. Owner-tuned Unifont expressions (font coverage proven via an off-panel render-to-PNG loop). Component-local deps (pillow/spidev/gpiozero/lgpio + apt liblgpio-dev/swig), 0 spine deps, suite 542 green, 3 contracts KEPT, no core/ change. Closes the panel-emulator-render-spike icebox. Follow-ons: epd.sleep() on teardown, STATUS_BAR compositing. Status → done.
