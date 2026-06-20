# Story 7.5 — Mood-nudge: a bounded plugin→core affect channel

**Date:** 2026-06-19
**Epic:** 7 (Extensibility & Optional Embodiment)
**Status:** design approved — feeds `bmad-create-story 7.5`

## Problem

Epic 7 gave plugins the ability to emit broadcast events (7.2) and physical
sensing plugins that emit `button-pressed` / `presence-arrived` / `presence-left`
(7.4). But there is **no path for any plugin to move the pet's mood/face**:

- Plugins never import `core/` (import-linter contract, AD-8).
- `ProposedOp` is the worker's reply channel only; plugins have no `Result` path.
- `Event` carries only an `EventKind` (no payload), and broadcast `EVENT` routes
  only to `PLUGIN_HOST` — core emits events but does not consume them.

So a button press cannot make the face react. 7.5 closes this gap with a general,
bounded **plugin→core affect channel** while keeping core the sole writer of
mood/face (AD-5) and the LLM-free invariant intact.

## Decisions (locked during brainstorming, 2026-06-19)

1. **Scope = general, not sensing-specific.** Any plugin can nudge the mood;
   the 7.4 sensing plugins are simply the first users.
2. **Semantic kinds, core maps magnitude.** Plugins emit an *affect* meaning;
   core decides how much the soul actually moves. No free-numeric payload
   crosses into core.
3. **Plugin emits affect (`NUDGE_*`), not facts.** `button-pressed` (fact) and
   `NUDGE_EXCITED` (affect) stay separate event kinds. Core's map is purely
   affect→patch — it never learns what a "button" is.
4. **Per-kind cooldown + hard clamp** defends against a flood. The reflex
   baseline (3.2) provides decay for free.

## Design

### Data flow

```
plugin (e.g. sensing_button)
   on press:
     host.emit_event(BUTTON_PRESSED)   # fact   — other plugins may count it
     host.emit_event(NUDGE_EXCITED)    # affect — the face reacts
        │  Event(event=NUDGE_EXCITED), src=PLUGIN_HOST, dst=None (broadcast)
        ▼
hub._route  (broadcast branch)
        │  delivers EVENT to PLUGIN_HOST  (unchanged)
        │  AND to CORE                    (NEW — core is a 2nd broadcast subscriber)
        ▼
core event handler (reflex-tier; no arbiter, no fork)
     patch = reactions.compute_nudge_patch(NUDGE_EXCITED, mood, energy)  # None if unknown kind
     if patch is None: ignore                       # MESSAGE_ANSWERED / BUTTON_PRESSED fall here
     if now - last_nudge[kind] < COOLDOWN_S: ignore # 30s debounce, core's clock
     state.apply_patch(patch)                        # AD-5 sole writer; WRITABLE_PATHS validated
     last_nudge[kind] = now
     if fence.is_idle and arbiter.is_idle:
         _maybe_push_mood_face()                     # face re-selects NOW
        ▼
faces.select(valence, arousal, energy) → "excited"
StateSnapshot(region=FACE, face="excited") → Actor.DISPLAY   (existing path, unchanged)
```

Decay: once idle, the 3.2 reflex loop settles valence/energy back toward
baseline — no new decay code.

### Components

**1. Contracts (`shelldon/contracts/__init__.py`) — additive, NO `SCHEMA_VERSION` bump**
(7.4 precedent: additive `EventKind` values do not bump the schema.)

Four new generic affect `EventKind` values:

| Kind | Meaning |
|---|---|
| `NUDGE_POSITIVE` | something good — valence up |
| `NUDGE_NEGATIVE` | something bad — valence down |
| `NUDGE_EXCITED` | stimulation — arousal up + slight valence up |
| `NUDGE_CALM` | settle — arousal down |

`Event` body is unchanged (kind only).

**2. Hub routing (`shelldon/core/bus/server.py` `_route` EVENT branch, lines 129-142)**

The single structural change: the broadcast branch, which today delivers an
`EVENT` only to `PLUGIN_HOST`, **also enqueues it on `core_inbox`** so the
runtime main loop consumes it. Guarded by **`src != Actor.CORE`** so core does
NOT receive its own emitted events (e.g. `MESSAGE_ANSWERED`, src=CORE) back —
only plugin-emitted nudges (src=PLUGIN_HOST) reach core. The hub stays
**kind-agnostic** (it does not know which kinds core cares about); core's
reactions map filters. This is tighter than treating self-delivery as a no-op:
no wasted enqueue, no self-loop.

**3. New pure module `shelldon/core/reactions.py`** (mirrors `reflexes.py` /
`power.py` — LLM-free, no clock, no I/O)

- Closed `MAP: EventKind → affect deltas`.
- Magnitudes honor 7-4 D3's owner-locked **0.3 scale**, recast onto clean
  single-emphasis affect kinds: `NUDGE_POSITIVE → valence +0.3`,
  `NUDGE_NEGATIVE → valence -0.3`, `NUDGE_EXCITED → arousal +0.3, valence +0.1`,
  `NUDGE_CALM → arousal -0.3`. (7-4 D3 framed the nudge per-sensor — superseded
  here by the affect-kind model from this brainstorm; the 0.3 scale is kept.)
- `compute_nudge_patch(kind, mood, energy) -> dict | None`: looks up the kind
  (unknown → `None`), adds the kind's deltas to the current values, and
  **clamps** valence/arousal to `[-1, 1]` and energy to `[0, 1]`. Returns an
  **absolute-valued** patch (e.g. `{"mood.arousal": 0.65, "mood.valence": 0.40}`),
  matching `compute_reflex_patch`'s shape so it feeds `state.apply_patch`
  directly. Returns `None` when the patch would be a no-op (already at bound).

**4. Core event handler (`shelldon/core/runtime.py`)** — reflex-tier, no
arbiter/fork involvement:

- `_last_nudge: dict[EventKind, float]` cooldown ledger (core's monotonic clock,
  same idiom as the 5.2 turn cooldown).
- On a broadcast `EVENT`: compute patch → `None` ⇒ ignore; within cooldown ⇒
  ignore; else `apply_patch` + record `last_nudge[kind]` + re-render the face
  via the existing `_maybe_push_mood_face()` **only when idle**. Mid-turn, the
  turn's own face pushes win and the existing between-turn logic re-renders after
  the turn settles.

**5. Sensing plugin edits (`shelldon/plugins/sensing_button.py`,
`sensing_ble.py`)** — small + additive. Each declares the affect kind in
`manifest.emits` (validated by the host's `emit_event`) and emits it alongside
the existing fact:

| Sensor event | Fact (existing) | Affect (new) |
|---|---|---|
| button press | `BUTTON_PRESSED` | `NUDGE_EXCITED` |
| presence arrived | `PRESENCE_ARRIVED` | `NUDGE_POSITIVE` |
| presence left | `PRESENCE_LEFT` | `NUDGE_NEGATIVE` |

### Defaults (baked, tunable)

- Affect magnitude **0.3 scale** per nudge (per the map above; honors 7-4 D3).
- Per-kind cooldown **30s**.
- A nudge does **not** reset `last_interaction` — it moves mood only and does not
  touch the proactive idle clock (presence/button must not silently suppress the
  proactive loop through the mood channel).

### Unchanged (the proof surface)

No new `MsgKind` (reuses `EVENT`) · no `Event` body change · no `SCHEMA_VERSION`
bump · no fork, no LLM, no arbiter on the nudge path · display untouched ·
plugins still never import core (import-linter 3 contracts KEPT) · **zero
existing core mood/face writers altered** (reflex, turn-dispatch, `_push_face`).

## Testing

- **`reactions.py` pure-fn:** each `NUDGE_*` → expected clamped patch; clamp at
  the `[-1,1]` / `[0,1]` bounds; no-op at bound → `None`; unknown kind → `None`.
- **Core handler:** cooldown drops a repeat within the window, applies after it;
  face re-pushed when idle, deferred mid-turn; `MESSAGE_ANSWERED` and
  `BUTTON_PRESSED` arriving at core are no-ops.
- **Hub routing:** a broadcast `EVENT` is delivered to `CORE`.
- **Sensing plugins:** emit the `NUDGE_*` alongside the fact; the emit is
  validated against `manifest.emits` (undeclared emit rejected).
- **Contracts:** import-linter 3 contracts KEPT; `EVENT` round-trips the new kinds.
- **CAP (capstone):** a `NUDGE_EXCITED` event → arousal↑ → `faces.select`
  returns `excited` → `StateSnapshot(region=FACE, face="excited")` pushed to
  display — a plugin-emitted physical event visibly moves the pet's soul.

## Out of scope / follow-ons

- Energy nudges (a `NUDGE_*` touching `energy`) — only valence/arousal in v1.
- Plugin→plugin nudge consumption (other plugins reacting to `NUDGE_*`) — the
  only consumer in v1 is core.
- Tuning the per-sensor affect mapping or magnitudes from config — hardcoded v1.
