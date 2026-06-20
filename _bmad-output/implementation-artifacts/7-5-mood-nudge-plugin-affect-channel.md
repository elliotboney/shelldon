---
baseline_commit: be2448c
---
# Story 7.5: Mood-nudge — a bounded plugin→core affect channel

Status: done

<!-- The face-reaction follow-on split out of 7.4 (D3 / Open Q1, RESOLVED 2026-06-19). 7.4 kept its zero-core boundary; 7.5 is the deliberate CORE change that lets the soul move in response to plugin events. -->
<!-- NOT in epics.md originally — a brainstorm-born story (2026-06-19), like 5.0/7.0. Design spec: docs/superpowers/specs/2026-06-19-mood-nudge-design.md (owner-approved). -->
<!-- The four brainstorm decisions are LOCKED in Dev Notes (D1–D4). The only structural change to core is one guarded line in the hub broadcast branch + a reflex-tier handler; everything else is additive + a pure module. -->

## Story

As the owner,
I want any plugin to be able to nudge the pet's mood over the bus via semantic affect events,
so that physical and behavioral plugin events visibly move the pet's soul — its face reacts — while core stays the sole writer of mood/face (AD-5) and the LLM-free invariant holds.

**Why this story exists (split from 7.4):** Stories 7.1–7.4 built the whole plugin layer — contract, host, event fan-out (core→plugins), the draw seam (plugins→display), and the event-emit seam (plugins→bus). 7.4 deliberately left `core/` **byte-unchanged**, so a button press could be *observed* by another plugin but could **not** move the pet's face (Open Q1 / D3). 7.5 closes that: a **general, bounded plugin→core affect channel**. Plugins emit a semantic *affect* (`NUDGE_EXCITED`, not the fact `BUTTON_PRESSED`); core maps each affect to a small, clamped, cooldown-debounced mood patch and re-renders the face through the existing mood→face compositor. Core owns the magnitude; plugins own the meaning. Decay is **free** — Story 3.2's reflex loop already settles mood back to baseline when idle.

## Acceptance Criteria

### AC1 — Generic affect `EventKind`s (additive, no schema bump)

**Given** the closed broadcast event vocabulary (AD-11) — core emits some kinds, plugins emit others, all fan out identically
**When** the affect kinds are added
**Then** `shelldon/contracts/__init__.py` gains four generic affect values on the existing `EventKind` enum — `NUDGE_POSITIVE = "nudge-positive"`, `NUDGE_NEGATIVE = "nudge-negative"`, `NUDGE_EXCITED = "nudge-excited"`, `NUDGE_CALM = "nudge-calm"` (pure additive declarations; **no** `SCHEMA_VERSION` / `MsgKind` / `ROUTING_TABLE` / `Event`-body change — exactly the shape of 7.2's and 7.4's `EventKind` adds)
**And** these are *affect* kinds (a meaning, e.g. "get excited"), deliberately distinct from *fact* kinds (`BUTTON_PRESSED`, an event that happened) — a plugin emitting a fact and emitting an affect are two separate declared emits.

### AC2 — The hub delivers broadcast events to core (the one structural change)

**Given** today the broadcast branch of `BusServer._route` delivers an `EVENT` envelope **only** to `Actor.PLUGIN_HOST` (Story 7.2 D1); core *emits* events but never *consumes* them
**When** the broadcast branch runs
**Then** it **also enqueues the event on `self.core_inbox`** (the same queue the runtime main loop already drains for point-to-point CORE traffic), so core can react — **guarded by `env.src is not Actor.CORE`** so core does **not** receive its own emitted events back (e.g. its `MESSAGE_ANSWERED`, `src=CORE`); only externally-emitted events (a plugin nudge, `src=PLUGIN_HOST`) reach core
**And** the hub stays **kind-agnostic** — it does not know which kinds core cares about; the plugin-host fan-out path (7.2) is unchanged (still `_deliver_to(PLUGIN_HOST, …)` when a host is registered, still a debug-drop when absent); a broadcast with neither a host nor a core-relevant kind remains harmless.

### AC3 — `core/reactions.py`: the pure affect→patch map (LLM-free, clockless)

**Given** the single-writer rule (AD-5) and the LLM-free core invariant — the nudge mapping is pure policy, like `core/reflexes.py` and `core/power.py`
**When** `shelldon/core/reactions.py` is built
**Then** it exposes a **closed** `MAP: EventKind → affect deltas` and `compute_nudge_patch(kind, mood, energy) -> dict | None` — a pure function (no clock, no I/O) that: returns `None` for any kind **not** in the map; otherwise adds the kind's deltas to the current values, **clamps** valence/arousal to `[-1.0, 1.0]` and energy to `[0.0, 1.0]`, and returns an **absolute-valued** patch (e.g. `{"mood.arousal": 0.65, "mood.valence": 0.40}`) shaped exactly like `compute_reflex_patch`'s output so it feeds `state.apply_patch` directly; returns `None` when the result equals the current values (already at a bound — a no-op tick, mirroring the reflex EPSILON guard)
**And** the magnitudes honor 7-4 D3's owner-locked **0.3 scale**, recast onto clean single-emphasis affect kinds:

| Kind | Patch (delta) |
|---|---|
| `NUDGE_POSITIVE` | valence **+0.3** |
| `NUDGE_NEGATIVE` | valence **−0.3** |
| `NUDGE_EXCITED` | arousal **+0.3**, valence **+0.1** |
| `NUDGE_CALM` | arousal **−0.3** |

### AC4 — Core nudge handler: cooldown-debounced, reflex-tier (no arbiter / no fork)

**Given** a flood of nudges from a buggy or chatty plugin must not peg the mood (Q4 decision: per-kind cooldown + hard clamp)
**When** core drains a broadcast `EVENT` from `core_inbox`
**Then** a new reflex-tier handler runs entirely on the main loop — **no arbiter admission, no fork, no LLM, no budget** (it is a state nudge, like `_run_reflex_job`): it calls `reactions.compute_nudge_patch(kind, mood, energy)`; if `None` → ignore (this is also how core silently drops a `MESSAGE_ANSWERED`/`BUTTON_PRESSED` it happens to see); else it checks a **per-kind cooldown** (`_last_nudge: dict[EventKind, float]`, default **30 s**, on core's monotonic clock — the 5.2 cooldown idiom) and drops the nudge if the same kind fired within the window; otherwise it applies the patch via `state.apply_patch(patch)` (AD-5 sole writer; `WRITABLE_PATHS`-validated) and records `_last_nudge[kind] = now`
**And** after a successful apply it re-renders the face **only when idle** — it calls the existing `_maybe_push_mood_face()` guarded by the existing idle check (`fence.is_idle and arbiter.is_idle`); **mid-turn** the patch still applies but the face push is skipped (the turn's own `_push_face` thinking/reply wins; the existing between-turn `_maybe_push_mood_face()` re-renders the nudged mood once the turn settles)
**And** a nudge does **not** touch `state.last_interaction` — it moves mood only and never resets the proactive idle clock (a presence/button nudge must not silently suppress the proactive loop through the mood channel).

### AC5 — Sensing plugins emit affect alongside fact (the first user)

**Given** 7.4's sensing plugins emit *facts* (`BUTTON_PRESSED` / `PRESENCE_ARRIVED` / `PRESENCE_LEFT`)
**When** `shelldon/plugins/sensing_button.py` and `shelldon/plugins/sensing_ble.py` are updated
**Then** each declares its affect kind in `manifest.emits` (so the host's `emit_event` validation passes — an undeclared emit is dropped+logged, 7.4 AC1) and emits the affect **alongside** the existing fact on the same trigger:

| Trigger | Fact (existing, unchanged) | Affect (new) |
|---|---|---|
| button press | `BUTTON_PRESSED` | `NUDGE_EXCITED` |
| paired device arrives | `PRESENCE_ARRIVED` | `NUDGE_POSITIVE` |
| paired device leaves | `PRESENCE_LEFT` | `NUDGE_NEGATIVE` |

**And** this is the only change to the sensing plugins — the pair-first rule (7.4 AC3), the gated hardware sources, and the fact emits are untouched.

### AC6 — CAP: a plugin-emitted event visibly moves the pet's soul

**Given** the full path: plugin → `host.emit_event(NUDGE_EXCITED)` → hub broadcast branch → `core_inbox` → core nudge handler → `apply_patch` → mood→face compositor
**When** a `NUDGE_EXCITED` event is emitted (real `BusServer` + the host + a real or stub source) while the pet is idle
**Then** an end-to-end test proves the mood moved (arousal↑) and `faces.select(valence, arousal, energy)` now returns the excited face and a `StateSnapshot(region=Region.FACE, face="excited")` is pushed to `Actor.DISPLAY` — the soul reacted to a plugin event, off the same bus stream
**And** the boundary holds: `uv run lint-imports` **3 contracts KEPT** (plugins still never import `core`); `uv sync --locked` **0 new deps**; no new `MsgKind`/`Region`/`Event`-body field; **no `SCHEMA_VERSION` change**; the full suite green.

### Out of scope (explicit — do NOT do here)

- **Parametric / free-numeric nudges** (a plugin sending raw `valence_delta` numbers). Rejected in brainstorm Q2 — core owns magnitude via the closed affect map; no numeric payload crosses into core.
- **Energy nudges** — v1 affect kinds touch valence/arousal only. A `NUDGE_*` mapping onto `energy` is a later additive map entry.
- **Plugin→plugin nudge consumption** — the only consumer of `NUDGE_*` in v1 is core. (Other plugins *can* subscribe to them via the existing fan-out, but nothing is built to.)
- **Config-driven magnitudes / per-sensor affect remapping** — the map and the per-sensor affect assignment are hardcoded v1.
- **A new decay mechanism** — decay is the existing 3.2 reflex baseline-settle; do not add one.
- **Changing the `Event` body** — nudges ride the kind-only `Event`. No payload field.

## Tasks / Subtasks

- [x] **Task 1 — Affect `EventKind`s** (AC1)
  - [x] `contracts/__init__.py`: added `NUDGE_POSITIVE`/`NUDGE_NEGATIVE`/`NUDGE_EXCITED`/`NUDGE_CALM` to `EventKind` (additive only; no `SCHEMA_VERSION`/`MsgKind`/`Event`-body change).
  - [x] verify: `test_plugin_contract` closed-set test expanded to the 10 kinds; `Event` round-trips a nudge kind; `lint-imports` 3 KEPT.
- [x] **Task 2 — The pure map `core/reactions.py`** (AC3)
  - [x] New `shelldon/core/reactions.py`: closed `_NUDGE_DELTAS` map + `compute_nudge_patch(kind, valence, arousal) -> dict | None` (unknown→None; clamp valence/arousal∈[-1,1]; no-op-at-bound→None; absolute-valued patch shaped like `compute_reflex_patch`). LLM-free, no clock, no I/O. (Signature takes valence/arousal directly — v1 affects touch neither energy nor any other path.)
  - [x] verify (`test_reactions.py`, 9 pure-fn tests): each kind → expected clamped patch; absolute-valued; clamp pins at both bounds; at-bound → `None`; unknown kind → `None`.
- [x] **Task 3 — Hub broadcast → core (guarded)** (AC2)
  - [x] `core/bus/server.py` `_route` EVENT branch: after the PLUGIN_HOST delivery, `if env.src is not Actor.CORE: await self.core_inbox.put(env)`. PLUGIN_HOST path + no-host debug-drop unchanged.
  - [x] verify (`test_bus_routing.py` +2): a broadcast `EVENT` with `src=PLUGIN_HOST` lands on `core_inbox`; one with `src=CORE` does **not** (still reaches the host); the 7.2 fan-out test still passes.
- [x] **Task 4 — Core nudge handler (reflex-tier)** (AC4)
  - [x] `core/runtime.py`: main-loop `core_inbox` dispatch gained an `EVENT` branch → `_handle_nudge(env.body.event)`. Added `_last_nudge: dict[EventKind, float]`, `DEFAULT_NUDGE_COOLDOWN = 30.0` + injectable `nudge_cooldown`, and an injectable `monotonic` clock (default `time.monotonic`). `_handle_nudge`: `compute_nudge_patch` → falsy⇒return; per-kind cooldown ⇒ drop within window; else `apply_patch` + record + `await _maybe_push_mood_face()` (its existing idle guard). Does **not** touch `last_interaction`. No arbiter/fork/LLM/budget.
  - [x] verify (`test_nudge.py`): nudge applies the mapped patch + repushes the face when idle; 2nd same-kind nudge within 30 s dropped, past it applies; distinct kinds independent; mid-turn the patch applies but no face push; `MESSAGE_ANSWERED` is a no-op; `last_interaction` untouched.
- [x] **Task 5 — Sensing plugins emit affect** (AC5)
  - [x] `plugins/sensing_button.py`: `emits += (NUDGE_EXCITED,)`; each press emits `BUTTON_PRESSED` then `NUDGE_EXCITED`. `plugins/sensing_ble.py`: `emits += (NUDGE_POSITIVE, NUDGE_NEGATIVE,)`; a paired arrive emits `PRESENCE_ARRIVED`+`NUDGE_POSITIVE`, a leave emits `PRESENCE_LEFT`+`NUDGE_NEGATIVE`. Pair-first + gated sources untouched.
  - [x] verify (`test_sensing.py` +2, manifest asserts updated): a press emits both fact+affect; a paired arrive/leave emits both; the unpaired-never-tracked-or-logged security test (7.4 AC3) still holds.
- [x] **Task 6 — CAP end-to-end + boundary gate** (AC6)
  - [x] End-to-end (`test_nudge.py::test_cap_plugin_nudge_drives_the_face_to_excited`): real `BusServer` + `Core.run()` + a PLUGIN_HOST connection emitting `NUDGE_EXCITED` → hub → core handler → mood arousal↑ → a `StateSnapshot(region=FACE, face="excited")` reaches DISPLAY (idle pet). The soul reacted off the bus.
  - [x] verify: full suite **537 pass** / 3 skip (+20); `uv run lint-imports` 3 KEPT; `uv sync --locked` 0 dep changes; core diff = `bus/server.py` (+7), `runtime.py` (+42), new `reactions.py` — the deliberate, scoped core change.

## Dev Notes

### Brainstorm decisions (LOCKED 2026-06-19 — owner-approved, see the design spec)

- **D1 — General channel, not sensing-specific (brainstorm Q1).** Any plugin can nudge the mood; the sensing plugins are just the first user. So the wire is generic affect kinds, not "react to button-pressed."
- **D2 — Semantic kinds, core owns magnitude (Q2).** Plugins emit an *affect meaning* (`NUDGE_EXCITED`); core maps it to a bounded patch. **Rejected:** a parametric `(valence_delta, arousal_delta)` payload — it would let a plugin partly own the soul's dynamics and would put the first free-numeric value into core. Keeping the magnitude in `core/reactions.py` preserves AD-5 (core is the sole writer *and* the sole authority on how much the soul moves).
- **D3 — Affect ≠ fact (Q3).** The sensing plugin emits both `BUTTON_PRESSED` (a fact other plugins may count) and `NUDGE_EXCITED` (an affect core reacts to). Core's map is purely affect→patch — it never learns what a "button" is. **Rejected:** core subscribing to the literal sensing kinds (would hardcode sensing knowledge into core and make "any plugin can nudge" mean "only the kinds core already knows").
- **D4 — Per-kind cooldown + hard clamp (Q4).** A flood of one kind applies once per 30 s window; every patch is clamped to the mood bounds; decay is the free 3.2 reflex settle. **Rejected:** clamp-only (a tight loop pins mood at the ceiling until it stops) and a token-bucket accumulator (more state than warranted).

### This supersedes 7-4 D3

7-4's D3 / Open Q1 pre-locked a face-reaction *as core mapping the sensing facts directly* (`presence-arrived → valence +0.3/arousal +0.2`, etc.). The brainstorm **supersedes the mechanism** (plugins emit `NUDGE_*`; core maps affect, not facts) while **keeping the 0.3 magnitude scale**. The per-sensor feel is now expressed through the affect assignment in AC5 + the map in AC3. (Net: `presence-arrived` becomes `NUDGE_POSITIVE` = valence +0.3; the small arousal bump 7-4 imagined is dropped for clean single-emphasis kinds — a plugin wanting both could emit `NUDGE_POSITIVE` + `NUDGE_EXCITED`, but v1 emits one.)

### The one structural core change, and why it's safe

Core today is a hub *and* a source (it `deliver`s OUTBOUND/STATE_SNAPSHOT and `_emit_event`s broadcasts) but it is **not** a broadcast *consumer*. AC2 makes it one by feeding broadcast events onto the existing `core_inbox` queue (unbounded `asyncio.Queue`, so the enqueue never blocks the emitter). The `src != Actor.CORE` guard means the only new traffic core sees is externally-originated events — today that is exactly plugin nudges. The handler is reflex-tier: it mutates state through the same single-writer `apply_patch` the reflex loop uses and never touches the arbiter, the fork, the LLM, or the budget. So the blast radius is: one guarded line in `_route`, one new main-loop branch, one new pure module. The chat/turn path is untouched.

### Verified seams (line refs — read these before editing)

- **Hub broadcast branch** — `shelldon/core/bus/server.py:128-147` (`_route`): EVENT→PLUGIN_HOST at 138-142; the point-to-point `dest is Actor.CORE → core_inbox.put` at 144-145 is the precedent for the new enqueue. `core_inbox` is constructed as an unbounded `asyncio.Queue[Envelope]` in `BusServer.__init__`.
- **Core emits an event** — `shelldon/core/runtime.py:465-482` (`_emit_event`), called at `:400` (`MESSAGE_ANSWERED` after slot release). These carry `src=Actor.CORE` → the AC2 guard keeps them out of `core_inbox`.
- **The reflex-tier model to mirror** — `shelldon/core/runtime.py:599-609` (`_run_reflex_job`): computes a patch via a pure policy module, applies via `state.apply_patch`, then `await self._maybe_push_mood_face()` at `:604`. The nudge handler is the same shape with a cooldown gate.
- **Mood → face** — `shelldon/core/faces.py` `select_face`/`FaceRegistry.select`; `_maybe_push_mood_face()` (`runtime.py:611-621`) pushes a `StateSnapshot(region=FACE)` to DISPLAY only when idle and only when the token changed. The nudge reuses it verbatim.
- **State + writable paths** — `shelldon/core/state.py`: `Mood(valence, arousal)` (`:33-38`), `PersonalityState` (`:52-60`), `apply_patch` + closed `WRITABLE_PATHS` (`:109-125`). `mood.valence`/`mood.arousal`/`energy` are writable; the nudge patch uses those keys.
- **Cooldown idiom** — the 5.2 turn cooldown (`BudgetGate`/`turn_cooldown`, `runtime.py:222`) is the per-kind-debounce precedent (monotonic clock, injectable constant).
- **Affect emit on the plugin side** — `shelldon/plugins/host.py` `_HostHandle.emit_event` (validates `kind in manifest.emits`); `shelldon/plugins/sensing_button.py` / `sensing_ble.py` (where the fact is emitted — add the affect emit beside it); `shelldon/plugins/manifest.py` `PluginManifest.emits`.

### Testing standards summary

- `uv run pytest -q` (offline). Pure-fn tests for `reactions.py` (no harness). Handler + routing tests reuse the in-process `BusServer`/`core_inbox` harness; drive the cooldown with an injectable/monotonic clock seam (or `_last_nudge` manipulation) rather than real sleeps where possible. The CAP test uses the real `BusServer` + host + a stub `ButtonSource` and asserts on the `StateSnapshot(region=FACE)` reaching DISPLAY.
- Success = AC1–AC6 covered; the diff is additive + the one guarded `_route` line + the new module + the handler; `lint-imports` 3 KEPT; `uv sync --locked` 0 new deps; `SCHEMA_VERSION` unchanged; full suite green.

### Project Structure Notes

- New: `shelldon/core/reactions.py` (pure policy, joins `reflexes.py`/`power.py`/`proactive.py`). Modified: `shelldon/contracts/__init__.py` (additive `EventKind`), `shelldon/core/bus/server.py` (1 line), `shelldon/core/runtime.py` (handler + `_last_nudge` + constant), `shelldon/plugins/sensing_button.py` + `shelldon/plugins/sensing_ble.py` (affect emit + `manifest.emits`). **This is the deliberate core change 7.4 deferred** — unlike 7.1–7.4, this story DOES touch `core/`. That is expected and is the whole point of 7.5.
- `runtime.py` coupling watch (Epic 5/6 iceboxed `runtime-dispatch-extract` already addressed dispatch): the nudge handler is small and reflex-shaped; keep it next to `_run_reflex_job`. If it grows, the pure mapping is already isolated in `reactions.py`.

### References

- [Source: docs/superpowers/specs/2026-06-19-mood-nudge-design.md] — the owner-approved design (brainstorm decisions D1–D4, data flow, the `src != CORE` guard, the affect map, testing).
- [Source: _bmad-output/implementation-artifacts/7-4-optional-physical-sensing-button-ble-presence.md#AC4 / Dev Notes D3 / Open Q1] — the split point: 7.4 proved the observable reaction via a subscribing plugin and explicitly deferred the FACE reaction (mood nudge) to Story 7.5 to keep its zero-core boundary; the pre-affect-model mapping this story supersedes.
- [Source: _bmad-output/implementation-artifacts/7-2-broadcast-event-subscriptions.md] — the `Event` broadcast + the hub EVENT branch (PLUGIN_HOST-only today) that AC2 extends to core.
- [Source: ARCHITECTURE-SPINE.md#AD-5] — single-writer state; core is the sole writer of mood/face. The nudge applies through `apply_patch`; plugins never write state directly.
- [Source: ARCHITECTURE-SPINE.md#AD-11] — closed broadcast event vocabulary; new kinds are declared, never self-registered at runtime.
- [Source: shelldon/core/runtime.py, shelldon/core/bus/server.py, shelldon/core/state.py, shelldon/core/faces.py, shelldon/core/reflexes.py] — the seams listed above.

### Open questions for the owner (do not block dev — defaults chosen)

1. **`NUDGE_CALM` ships unused.** v1 sensing emits POSITIVE/NEGATIVE/EXCITED only; `NUDGE_CALM` is defined for "any plugin can nudge" generality but has no emitter. Keep it (recommended — completes the affect quadrant, cheap) or drop it until something emits it?
2. **`presence-arrived` feel.** v1 maps it to `NUDGE_POSITIVE` (valence only). 7-4 D3 also wanted a small arousal bump ("excited to see you"). Leave as POSITIVE-only (recommended, clean) or have the BLE plugin emit POSITIVE + EXCITED on arrival?
3. **Cooldown value.** 30 s per kind (matches the 5.2 idiom). For a button you press repeatedly, 30 s means only the first press in a window moves the mood — intended (anti-spam) but confirm it's not surprising for a deliberate tap.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Baseline: `uv run pytest -q` → 517 passed, 3 skipped (includes the uncommitted 7.4 work).
- Post-change gate: `uv run pytest -q` → **537 passed, 3 skipped, 5 deselected** (+20); `uv run lint-imports` → 3 contracts KEPT, 0 broken; `uv sync --locked` → 0 dep changes.
- One TDD detour worth recording: the CAP end-to-end first failed because the test `_`-discarded the DISPLAY `StreamWriter` from `connect()`; a dropped writer is GC'd and closes the UDS connection, which deregisters the actor mid-test. Fix = keep both writers referenced. (Not a product bug — a test-harness lifetime gotcha; noted for future bus e2e tests.)

### Completion Notes List

- **The plugin→core affect channel is live (CAP proven).** A plugin emits a semantic affect (`NUDGE_*`); the hub now delivers broadcast events to core (guarded `src != CORE`); a reflex-tier handler maps the affect to a bounded, clamped, cooldown-debounced mood patch via the new pure `core/reactions.py`; the existing 3.3 mood→face compositor re-renders. End-to-end: a `NUDGE_EXCITED` on the bus drives an `excited` FACE snapshot to the display.
- **Core owns the magnitude (D2).** The affect→delta table lives in `core/reactions.py`, not in any plugin — a plugin chooses the *meaning*, core decides how far the soul moves. No free-numeric payload crosses into core; AD-5 keeps core the sole authority over affect dynamics, not just the sole writer.
- **The one structural core change is tiny + guarded.** `_route`'s EVENT branch gained `if env.src is not Actor.CORE: await self.core_inbox.put(env)` — core became a second broadcast consumer. The `src != CORE` guard means core never re-consumes its own `MESSAGE_ANSWERED` (no self-loop, no wasted enqueue). The hub stays kind-agnostic; the reactions map is the only thing that decides which kinds move mood.
- **Reflex-tier, no turn machinery.** `_handle_nudge` mutates state through the single-writer `apply_patch` and re-renders the face via the existing `_maybe_push_mood_face()` (its idle guard already defers a push while a turn is in flight — the mood still moves, the face catches up after the turn). It never touches the arbiter, fork, LLM, budget, or `last_interaction` (a nudge moves mood, not the proactive idle clock).
- **Decay was free (D4).** No new decay code — the Story 3.2 reflex baseline-settle already pulls a nudged mood back toward neutral when idle. The per-kind 30 s cooldown (injectable, 5.2 idiom) + the hard clamp are the only anti-spam state.
- **Magnitudes reconcile 7-4 D3.** The 0.3 scale 7-4 locked is preserved, recast onto clean single-emphasis affect kinds (POSITIVE/NEGATIVE = valence; EXCITED = arousal-led +0.1 valence; CALM = -arousal). `NUDGE_CALM` ships defined-but-unused by the sensing plugins (Open Q1) — available for any future plugin.
- **This story DOES touch `core/` — by design.** Unlike 7.1–7.4 (zero-core), 7.5 is the deliberate core change 7.4 deferred (its D3 / Open Q1). The boundary that still holds: plugins never import core (import-linter 3 KEPT), no new `MsgKind`/`Region`/`Event`-body field, no `SCHEMA_VERSION` bump, 0 new deps.

### File List

- `shelldon/contracts/__init__.py` — MODIFIED. `EventKind` += `NUDGE_POSITIVE`/`NUDGE_NEGATIVE`/`NUDGE_EXCITED`/`NUDGE_CALM` (additive; no schema bump).
- `shelldon/core/reactions.py` — NEW. The pure affect→mood-patch policy: closed `_NUDGE_DELTAS` + `compute_nudge_patch(kind, valence, arousal) -> dict | None` (clamped, absolute-valued, clockless, LLM-free).
- `shelldon/core/bus/server.py` — MODIFIED. `_route` EVENT branch also enqueues a non-core-originated broadcast on `core_inbox` (core is now a broadcast consumer; `src != CORE` guard).
- `shelldon/core/runtime.py` — MODIFIED. `DEFAULT_NUDGE_COOLDOWN`; `nudge_cooldown` + injectable `monotonic` ctor params; `_last_nudge` ledger; an `EVENT` branch in the main loop; the reflex-tier `_handle_nudge`. `import time` + `compute_nudge_patch`.
- `shelldon/plugins/sensing_button.py` — MODIFIED (7.4-owned file). `emits += NUDGE_EXCITED`; a press emits the fact then the affect.
- `shelldon/plugins/sensing_ble.py` — MODIFIED (7.4-owned file). `emits += NUDGE_POSITIVE/NUDGE_NEGATIVE`; arrive/leave emit the fact then the affect.
- `tests/test_reactions.py` — NEW. 9 pure-fn tests for the affect map.
- `tests/test_nudge.py` — NEW. 7 tests: handler apply/clamp/cooldown/independence/mid-turn/unknown-kind/`last_interaction`-untouched + the CAP end-to-end.
- `tests/test_bus_routing.py` — MODIFIED. +2: plugin event reaches `core_inbox`; core's own event is not echoed back.
- `tests/test_plugin_contract.py` — MODIFIED. The closed-set assertion expanded to the 4 affect kinds.
- `tests/test_sensing.py` — MODIFIED (7.4-owned file). +2 fact+affect emit tests; manifest `emits` asserts updated.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `7-5 → in-progress → review`.
- `docs/superpowers/specs/2026-06-19-mood-nudge-design.md` — NEW (created in the brainstorm/spec step).

### Review Findings

- [x] [Review][Defer] Float equality `new_v != valence` after FP arithmetic [reactions.py:53-54] — deferred, pre-existing
- [x] [Review][Defer] Spawned task calling `host.spawn()` from within itself races with teardown's `gather(*tasks)` capture [host.py:~270-294] — deferred, pre-existing 7.4 API design, no current plugin violates this
- [x] [Review][Defer] Spec text inconsistency: AC3/AC4 reference `(kind, mood, energy)` signature and energy clamp — impl correctly omits energy (v1 doesn't touch it); spec text should be aligned — deferred, spec cleanup

### Change Log

- 2026-06-19 — Story 7.5 implemented: the bounded plugin→core affect channel. New affect `EventKind`s (`NUDGE_*`) + a pure `core/reactions.py` (affect→clamped-mood-patch, 0.3 scale) + the hub delivering broadcast events to core (guarded `src != CORE`) + a reflex-tier `_handle_nudge` (per-kind 30 s cooldown, single-writer apply, mood→face re-render, no arbiter/fork/LLM/budget). The 7.4 sensing plugins now emit an affect alongside each fact (button→excited, arrive→positive, leave→negative). CAP proven end-to-end (a plugin's `NUDGE_EXCITED` drives an `excited` face to the display). The first Epic 7 story to touch `core/` — deliberate (7.4's deferred D3). +20 tests, suite **537 pass**, 3 import contracts KEPT, 0 new deps, no schema bump. Status → review.

- 2026-06-19 — Story 7.5 created (brainstorm-born, not in original epics.md): the bounded plugin→core affect channel that lets plugin events move the pet's face — the face-reaction follow-on split out of 7.4. Design via the superpowers brainstorming skill (4 locked decisions); spec at docs/superpowers/specs/2026-06-19-mood-nudge-design.md. Status → ready-for-dev.
