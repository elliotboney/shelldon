---
baseline_commit: be2448c
---
# Story 7.4: Optional physical sensing (button / BLE presence)

Status: done

<!-- The LAST Epic 7 story — the optional-embodiment capstone (CAP-3). Hardware-GATED: the PiSugar2 button + BLE scan run only on the Pi; the laptop suite proves the mechanism through injected stubs (same "ship the mechanism + seam, gate the hardware" discipline as the fork uid-drop and the E-Ink renderer). -->
<!-- Forces the deferred half: plugins EMITTING events (sensing -> bus). 7.2 = core emits / plugins consume; 7.3 = plugins draw; 7.4 = plugins emit events. -->
<!-- Scoping decisions D1–D6 are explicit in Dev Notes. The ONE big owner decision (does the pet's FACE react?) is D3 + Open Q1. -->

## Story

As the owner,
I want optional sensing plugins — a PiSugar2 button and BLE presence of my paired device — that emit events when something physical happens,
so that, if I add the hardware, the pet reacts to presence and touch — while the chat-bot pet keeps working fully without any of it (CAP-3).

**Why this is the Epic-7 capstone:** 7.1–7.3 built the plugin contract, the host, event fan-out (core→plugins), and the draw seam (plugins→display). The one half still missing is **plugins emitting events onto the bus** — the `emits` manifest field was declared in 7.1 and has stayed unconsumed. Physical sensing is its natural first user: a button press or a paired device arriving is a *plugin-originated* event the rest of the system can react to. This story builds the **plugin event-emit seam** (validated against `manifest.emits`, AD-11) and the two sensing plugins, with the real hardware (PiSugar2 HTTP/socket API, `bleak` BLE) **gated** so everything is provable on a laptop with injected stubs. **BLE is pair-first** — only previously-paired devices are ever tracked (AD-8 security rule).

## Acceptance Criteria

### AC1 — Sensing event kinds + the plugin event-emit seam (plugins → bus)

**Given** AD-8: a plugin may *emit* event envelopes; `manifest.emits` (declared 7.1) is the closed, manifest-declared set it is allowed to emit (AD-11: no runtime self-registration of new kinds)
**When** the emit seam is built
**Then** `shelldon/contracts/__init__.py` gains the closed sensing kinds on the existing `EventKind` enum — `BUTTON_PRESSED = "button-pressed"`, `PRESENCE_ARRIVED = "presence-arrived"`, `PRESENCE_LEFT = "presence-left"` (pure additive declarations, no `SCHEMA_VERSION`/`MsgKind`/`ROUTING_TABLE` change — same shape as 7.2's `EventKind` add)
**And** the host hands each plugin (via `on_start`) a way to **emit** a broadcast event: `emit_event(kind: EventKind)` that the host **validates against `plugin.manifest.emits`** (an undeclared kind is logged + dropped — a plugin can only emit what it declared, mirroring the draw seam's region guard), then publishes an `Event(kind)` envelope (`src=PLUGIN_HOST`, `dst=None`) on the host's writer → the hub broadcast branch (7.2) → fans out to every subscribed plugin
**And** the draw seam (`draw(region, face)`, 7.3) and the new event seam (`emit_event(kind)`) are exposed together — `on_start(self, host)` now receives a small **host handle** carrying both (D1), replacing 7.3's bare `emit` callable; `BasePlugin` stores it; the 7.3 XP plugin is updated to `host.draw(...)`.

### AC2 — A PiSugar2 button sensing plugin (gated hardware, injected source)

**Given** "ship the mechanism + seam, gate the hardware" (the fork uid-drop / E-Ink precedent) — PiSugar2's button is read over its **local HTTP/socket API**, a component-local install-time dep, NOT a spine dep
**When** `shelldon/plugins/sensing_button.py` runs
**Then** it declares `MANIFEST` with `emits=(BUTTON_PRESSED,)` and a claimed resource (e.g. `resources=("pisugar:button",)`), and runs a background sense loop (started in `on_start`) that awaits an **injectable** `ButtonSource` (an async iterator of press events); on each press it calls `host.emit_event(EventKind.BUTTON_PRESSED)`
**And** the real PiSugar2 source (its HTTP/socket API) is a `# pragma: no cover` adapter behind the `ButtonSource` seam, imported lazily so the laptop suite never needs it — tests inject a stub source and assert a stubbed press produces a `BUTTON_PRESSED` event on the bus. (D4/D6.)

### AC3 — A BLE presence plugin that is pair-first (the security AC)

**Given** AD-8: "BLE presence is **pair-first** — only previously-paired devices are tracked, **no promiscuous scanning**" (pairing UX deferred)
**When** `shelldon/plugins/sensing_ble.py` runs
**Then** it declares `MANIFEST` with `emits=(PRESENCE_ARRIVED, PRESENCE_LEFT)` and a claimed resource (e.g. `resources=("ble:adapter",)`), is configured with a set of **previously-paired** device ids (its own private config — pairing UX deferred, D5), and runs a background presence loop over an **injectable** `PresenceSource`; it emits `PRESENCE_ARRIVED` when a *paired* id transitions absent→present and `PRESENCE_LEFT` on present→absent
**And** the pair-first rule is enforced as **pure logic**: a device id **not** in the paired set is **never** emitted, **never** logged, and never influences state — there is no code path that records or reports an arbitrary nearby device (a test asserts an unpaired id in the scan produces no event and no log line)
**And** the real `bleak` scanner is a `# pragma: no cover` adapter behind the `PresenceSource` seam, lazily imported (so `bleak` is **not** a hard dep — `uv sync --locked` stays 0-new-deps); tests inject a stubbed scan.

### AC4 — An observable reaction (the event reaches subscribers)

**Given** the full emit path: sensing plugin → `host.emit_event` → hub broadcast → fan-out to subscribers
**When** a `BUTTON_PRESSED` (or `PRESENCE_ARRIVED`) event is emitted with a plugin subscribed to that kind loaded
**Then** an end-to-end test (real `BusServer` + the host) proves the subscribed plugin's `on_event` fires with the sensing event — the reaction is demonstrably driven off the same bus stream
**And** **core / the pet's FACE** reacting to a sensing event is **out of 7.4 by design — it is Story 7.5** (owner decision 2026-06-19, Open Q1 RESOLVED): the face WILL react, via a **reflex-tier, LLM-free mood nudge** (`presence-arrived → valence +0.3/arousal +0.2`, `button-pressed → arousal +0.3`, `presence-left → arousal −0.2`), but that requires the hub to deliver sensing events to `core` + a reflex handler (a core change), so it lives in its own story to keep 7.4 the pure zero-core plugin layer.

### AC5 — CAP-3 optionality + the boundary holds

**Given** CAP-3: the sensing plugins are optional
**When** they are absent
**Then** the pet functions fully — the chat path (inbound → turn → reply → face) is untouched, and `core/` is byte-unchanged by this story (the sensing plugins live entirely in `shelldon/plugins/`)
**And** `uv run lint-imports` stays green — **3 contracts KEPT** (the sensing plugins import only `shelldon.contracts` + `shelldon.plugins.*`, never `shelldon.core`, never an LLM SDK); `uv sync --locked` 0 new deps (bleak/PiSugar gated); the full suite green.

### Out of scope (explicit — do NOT do here)

- **Core / the pet's FACE reacting to sensing events** (D3 / Open Q1) — needs the hub to route events to `core` + a core handler. A deliberate core feature; its own follow-on, not smuggled into the optional-sensing story.
- **Real PiSugar2 / `bleak` I/O on a laptop** — the hardware adapters are `# pragma: no cover`, lazily imported, exercised on the Pi (like the Linux-gated real-fork test + the real E-Ink driver). No hard dep added.
- **BLE pairing UX** — deferred (AD-8). The paired-id set is private plugin config; how a device gets paired is a later concern.
- **A general actuator/output path beyond the existing draw seam** — sensing is input-only here.
- **New `MsgKind` / `Region` / a non-`Event` wire message** — sensing reuses the 7.2 `Event` broadcast (just new `EventKind` values). No `SCHEMA_VERSION` touch.

## Tasks / Subtasks

- [x] **Task 1 — Sensing `EventKind`s + the host handle (`draw` + `emit_event`)** (AC1)
  - [x] `contracts/__init__.py`: added `BUTTON_PRESSED`/`PRESENCE_ARRIVED`/`PRESENCE_LEFT` to `EventKind` (additive; no other contract change).
  - [x] `plugins/manifest.py`: a `Host` Protocol (`draw` + `emit_event` + `spawn`); `on_start(self, host)`; `BasePlugin._host`. `plugins/host.py`: `_HostHandle` (draw = 7.3 region-scoped seam unchanged; `emit_event` validates `kind in manifest.emits` → log+drop, writes `Event(src=PLUGIN_HOST, dst=None)`; `spawn` = host-owned background task). `run_plugin_host` tracks + cancels spawned tasks on teardown. XP plugin → `self._host.draw(...)`.
  - [x] verify: `emit_event` of a declared kind reaches a subscriber via the hub round-trip; an undeclared kind is dropped+logged; the XP draw + all 7.1/7.3 tests still pass (updated for the handle).
- [x] **Task 2 — The button sensing plugin** (AC2)
  - [x] `shelldon/plugins/sensing_button.py`: `ButtonSource` (async iterator of presses), a `# pragma: no cover` lazy PiSugar2 adapter, `MANIFEST` (emits BUTTON_PRESSED, claims `pisugar:button`), `SensingButtonPlugin` whose `on_start` spawns a host-owned sense loop emitting on each press; `make_button_plugin(source=...)`; module `PLUGIN` idles with `source=None`.
  - [x] verify: a stub feeding 2 presses → 2 `BUTTON_PRESSED` events reach a subscriber; teardown cancels the sense loop cleanly; no-source → idle (no emits).
- [x] **Task 3 — The BLE presence plugin (pair-first)** (AC3)
  - [x] `shelldon/plugins/sensing_ble.py`: `PresenceSource` (async iterator of seen-id sets), a `# pragma: no cover` lazy `bleak` adapter, `MANIFEST` (emits PRESENCE_ARRIVED/LEFT, claims `ble:adapter`), `SensingBlePlugin(paired_ids, source)` — **pair-first filter FIRST** each scan, emits only on paired in/out transitions.
  - [x] verify: paired arrive → `PRESENCE_ARRIVED`, leave → `PRESENCE_LEFT`; stable presence → only the arrival edge; an unpaired id in every scan → **no event AND never logged** (caplog asserted free of the id).
- [x] **Task 4 — Observable reaction + optionality + gate** (AC4, AC5)
  - [x] AC4 reaction: the button/BLE tests run the real sensing plugin through the host and a *separate* subscriber plugin receives the emitted event (cross-plugin, off the bus).
  - [x] Optionality: the sensing plugins idle with no source (tested); the app smoke loads all three shipped plugins (xp + 2 idle sensing) and the chat turn still completes (CAP-3).
  - [x] verify: `git status -- shelldon/core/` **empty**; `uv run lint-imports` 3 KEPT; `uv sync --locked` 0 new deps; full suite **517 pass**.

## Dev Notes

### Scoping decisions (made, not assumed — flagged for the owner at the end)

- **D1 — `on_start` takes a host handle, not a bare callable.** 7.3 passed `emit` (the draw callable). 7.4 needs a second capability (`emit_event`), so the seam becomes a small handle: `on_start(self, host)` with `host.draw(region, face)` + `host.emit_event(kind)`. Small churn to the 7.3 XP plugin (`emit(...)` → `host.draw(...)`); cleaner and extensible. (Open Q3.)
- **D2 — Sensing kinds on the existing `EventKind` enum** (`button-pressed`, `presence-arrived`, `presence-left`). One closed broadcast vocabulary (AD-11); core emits some kinds, plugins emit others — all fan out identically. Additive, no schema bump.
- **D3 — The pet's FACE/core does NOT react in 7.4 — it's Story 7.5 (RESOLVED 2026-06-19).** 7.4's observable reaction is proven via a *subscribing plugin*; `core` stays byte-unchanged. The owner chose (Open Q1) that the face WILL react, via a **reflex-tier LLM-free mood nudge** — but since that needs the hub to deliver sensing events to `core` (the broadcast branch targets `PLUGIN_HOST` only today — 7.2 D1) + a reflex handler applying an `apply_patch`, it is split into **Story 7.5** so 7.4 keeps its zero-core boundary. Mapping locked: `presence-arrived → mood.valence +0.3, arousal +0.2`; `button-pressed → arousal +0.3`; `presence-left → arousal −0.2` (reuses the mood→face compositor, lingers + drifts back; no fork/LLM/budget).
- **D4 — Two plugins (button + BLE), not one combined.** They claim different resources and read different hardware; AD-8 conflict-rejection + clean separation favors two modules. Both are tested through injected stubs.
- **D5 — Paired-id set is private plugin config.** The BLE plugin is constructed with its paired device ids (its own state, pairing UX deferred per AD-8). Tests pass an explicit set.
- **D6 — Hardware is gated, 0 new hard deps.** The PiSugar2 HTTP/socket client and `bleak` are lazily imported inside `# pragma: no cover` adapters behind the `ButtonSource`/`PresenceSource` seams (component-local install-time deps, spine §Component-local). The laptop suite injects stubs; `uv sync --locked` stays 0-new-deps. (Add `bleak`/PiSugar to a component manifest when the hardware is in hand.)

### The new mechanism: plugins emit events (and the write-safety note)

7.2 made events flow core→plugins; 7.4 adds plugin→bus. A sensing plugin runs a **background sense-loop task** (started in `on_start`) that awaits its injected hardware source and calls `host.emit_event(kind)`. The host's `emit_event` writes an `Event` envelope on the host's single writer. Note: the host's read loop also writes (draws, via `on_event`→`host.draw`); now a plugin's sense-loop task writes too. This is **frame-safe** — `write_frame` does a synchronous `writer.write(full_frame_bytes)` (the whole length-prefixed frame is buffered atomically) before `await drain()`, so concurrent `write_frame` calls from different tasks never interleave at the byte level. Document this; it's the reason a background emitter is safe on the shared writer.

The emitted event round-trips through the hub: `host.emit_event` → `Event(src=PLUGIN_HOST, dst=None)` → hub broadcast branch (7.2) → delivered back to `PLUGIN_HOST` → the host's read loop fans it out to subscribed plugins. (Consistent single fan-out path; a sensing plugin subscribing to its own kind would hear itself — it won't.)

### Verified seams (line refs)

- `EventKind` (extend) + `Event` broadcast body + `dst=None` + the hub broadcast branch — `contracts/__init__.py` (`EventKind`, `Event`), `core/bus/server.py:_route` EVENT branch (7.2). No change to the hub needed (it already broadcasts any `Event` to `PLUGIN_HOST`).
- `PluginManifest.emits` (declared 7.1, consumed here) — `plugins/manifest.py`. `on_start`/`BasePlugin._emit`/`_make_emitter` (the draw seam to fold into the handle) — `plugins/host.py`, `plugins/manifest.py` (7.3).
- The gated-hardware precedent: `app.py` worker uid-drop (Linux-gated) + `display/renderer.py` (`StubRenderer` vs the real Waveshare driver) — copy that "Protocol seam + stub + `# pragma: no cover` real adapter" shape.
- Spine deps: `bleak@3.0.1` is a component-local install-time dep (spine §stack key_deps note + §Component-local deps), PiSugar2 over its local HTTP/socket API — both gated, not added to `pyproject` here.

### Testing standards summary

- `uv run pytest -q` (offline). Reuse the in-process `BusServer` harness + `sock_path`; inject stub `ButtonSource`/`PresenceSource` (async iterators) so no real hardware is touched. Sensing plugins constructed via factories with the stub source + (BLE) an explicit paired-id set. The pair-first test asserts an unpaired id yields **no event and no log**.
- Success = AC1–AC5 covered; **no `shelldon/core/` change**; `lint-imports` 3 KEPT; `uv sync --locked` 0 new deps; full suite green. The sensing plugins' real hardware adapters are `# pragma: no cover` (exercised only on the Pi).

### Project Structure Notes

- New: `shelldon/plugins/sensing_button.py`, `shelldon/plugins/sensing_ble.py`. Modified (plugins only): `shelldon/plugins/manifest.py` + `shelldon/plugins/host.py` (the `on_start` host handle + `emit_event`), `shelldon/plugins/xp.py` (`emit` → `host.draw`). **No `shelldon/core/` change.** No new hard dependency.
- Default-on vs default-off: like the XP plugin, a module in `shelldon.plugins` is auto-discovered. A sensing plugin with **no hardware source configured** should idle (emit nothing) rather than crash — decide whether they self-disable when their hardware/source is absent (Open Q2).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 7.4] — sensing plugin(s): button/BLE → event emitted → observable reaction; BLE pair-first (only previously-paired tracked, no promiscuous scan); absent → chat pet works fully (CAP-3).
- [Source: ARCHITECTURE-SPINE.md#AD-8] — one plugin contract for hardware + behavioral; plugins emit/subscribe events, claim GPIO/BLE; **BLE pair-first, no promiscuous scanning**; a crashed plugin kills only its own sensing.
- [Source: ARCHITECTURE-SPINE.md#stack + #Component-local deps] — `bleak@3.0.1` (BLE, Linux/BlueZ, plugin-host) + PiSugar2 over local HTTP/socket API are component-local install-time deps, gated to the hardware — not spine invariants.
- [Source: _bmad-output/implementation-artifacts/7-2-…subscriptions.md, 7-3-…optional.md] — the `Event` broadcast + hub branch (7.2) the emit seam reuses; the `on_start` draw seam (7.3) the host handle extends.
- [Source: shelldon/plugins/manifest.py, shelldon/plugins/host.py, shelldon/plugins/xp.py] — `emits` field, `on_start`/draw seam, the XP plugin to update.
- [Source: shelldon/display/renderer.py, shelldon/app.py] — the Protocol-seam + stub + `# pragma: no cover` real-adapter pattern to mirror for the hardware sources.

### Open questions for the owner (do not block dev — defaults chosen above)

1. ~~Does the pet's FACE react?~~ **RESOLVED 2026-06-19:** yes — via a reflex-tier LLM-free **mood nudge**, implemented in **Story 7.5** (kept out of 7.4 to preserve its zero-core boundary). See D3 for the locked mapping.
2. **Default-on or default-off?** Auto-discovered like XP, but sensing needs hardware. Should the sensing plugins self-disable when no source is configured (recommended — idle, emit nothing), or only load when explicitly enabled (env/config flag)?
3. **Host-handle refactor (D1):** fold `draw` + `emit_event` into one `host` handle passed to `on_start` (recommended, small XP churn) vs keep `on_start(emit)` for draw and bind `emit_event` separately (less churn, two seams)?
4. **One sensing plugin or two (D4)?** Two (button + BLE) recommended — or a single `sensing.py` if you'd rather keep it one module?

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Baseline: `uv run pytest -q` → 508 passed, 3 skipped.
- Post-change gate: `uv run pytest -q` → **517 passed, 3 skipped, 5 deselected** (+9); `uv run lint-imports` → 3 contracts KEPT, 0 broken; `uv sync --locked` → 0 dep changes; `git status -- shelldon/core/` → **empty (zero core changes)**.

### Completion Notes List

- **The plugin event-emit half is now live** — the `emits` manifest field (declared in 7.1, unconsumed) finally has a user. A plugin emits via `host.emit_event(kind)` (validated against `manifest.emits`); the host writes an `Event(src=PLUGIN_HOST, dst=None)` that round-trips through the hub broadcast branch (7.2) back to the host, which fans it out to subscribers. Proven end-to-end (a button plugin's press reaches a separate subscriber plugin).
- **`on_start` now hands a host handle, not a bare callable (D1).** `Host` = `draw` (7.3, unchanged) + `emit_event` (7.4) + `spawn` (host-owned background task). `spawn` is what makes a sensing producer loop safe: the host tracks every spawned task and cancels them on teardown, so a sense loop never leaks. Small churn folding 7.3's `emit` → `host.draw` (XP plugin + the 7.1/7.3 test fakes updated).
- **Frame-safe concurrent writes.** A draw (from the read loop's `on_event`) and an emit (from a sense-loop task) both `write_frame` on the host's single writer — safe because `write_frame` buffers the whole length-prefixed frame synchronously before awaiting drain, so frames never interleave at the byte level. Documented on `_HostHandle`.
- **BLE pair-first is enforced as the first thing each scan does (the security AC).** `seen = {d for d in scan if d in paired}` runs before any emit or log — an unpaired id never touches `present`, an event, or a log line. Asserted: an unpaired id present in every scan produces no event and the captured logs are free of the id.
- **Hardware gated, 0 new hard deps (D6).** The real PiSugar2 (HTTP/socket) and `bleak` scanners are `# pragma: no cover` adapters that `raise NotImplementedError` until wired on the Pi; the laptop suite injects stub sources. No `bleak`/PiSugar dep added — `uv sync --locked` unchanged.
- **On by default, idle without hardware (Q2 default).** All three shipped plugins (xp + the 2 sensing) auto-discover; a sensing plugin with `source=None` logs "idling (CAP-3)" and emits nothing, so shipping them on is safe — the chat pet is unaffected (app smoke still completes a turn with them loaded).
- **Zero `core/` changes** — the whole story lives in `shelldon/plugins/` + the additive `EventKind`s. The pet's FACE reacting (mood nudge) is **Story 7.5** (owner decision, Open Q1) — deliberately kept out so 7.4 holds its zero-core boundary.

### File List

- `shelldon/contracts/__init__.py` — MODIFIED. `EventKind` += `BUTTON_PRESSED`/`PRESENCE_ARRIVED`/`PRESENCE_LEFT` (additive; no schema bump).
- `shelldon/plugins/manifest.py` — MODIFIED. `Host` Protocol (draw + emit_event + spawn); `on_start(self, host)`; `BasePlugin._host`. Dropped the dead `Emit` alias.
- `shelldon/plugins/host.py` — MODIFIED. `_HostHandle` (replaces `_make_emitter`): region-scoped `draw`, manifest-validated `emit_event`, host-owned `spawn`; `run_plugin_host` tracks + cancels spawned tasks on teardown; `import asyncio`.
- `shelldon/plugins/xp.py` — MODIFIED. `self._emit(...)` → `self._host.draw(...)`.
- `shelldon/plugins/sensing_button.py` — NEW. PiSugar2 button plugin (gated source, emits BUTTON_PRESSED).
- `shelldon/plugins/sensing_ble.py` — NEW. BLE presence plugin, pair-first (gated `bleak` source, emits PRESENCE_ARRIVED/LEFT).
- `tests/test_sensing.py` — NEW. 7 tests: button presses→events + idle + teardown; BLE arrive/leave + stable-edge + **unpaired never tracked/logged** + manifests.
- `tests/test_plugin_host.py` — MODIFIED. +2 emit-seam tests; `_Drawer` → host handle; discovery test now expects the 3 shipped plugins.
- `tests/test_xp_plugin.py` — MODIFIED. `_CapturingEmit` → a fake host handle (`draw`/`emit_event`/`spawn`).
- `tests/test_plugin_contract.py` — MODIFIED. `EventKind` closed-set test includes the sensing kinds.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `7-4 → in-progress → review`.

### Review Findings

**Patches:**
- [x] [Review][Patch] `XpPlugin.on_start` parameter still named `emit` — rename to `host` to match Host Protocol contract [shelldon/plugins/xp.py:124-126]

**Deferred:**
- [x] [Review][Defer] Sense loops have no try/except around `emit_event` — silent task death on write error [sensing_ble.py:56-59, sensing_button.py:49-50] — deferred, hardware not wired yet; all shipped PLUGINs have source=None so the loop never runs
- [x] [Review][Defer] `spawn()` called from `on_event` post-teardown could leak a task [host.py:221-223] — deferred, no current plugin calls spawn from on_event; theoretical race
- [x] [Review][Defer] `gather(return_exceptions=True)` on teardown swallows all task exceptions silently [host.py:291-294] — deferred, consistent with existing codebase teardown pattern
- [x] [Review][Defer] No timeout on `on_start` — a deadlocked hardware plugin blocks all subsequent plugins and the read loop [host.py:272-273] — deferred, pre-existing since 7.3; fix requires design decision (timeout value, recovery path)
- [x] [Review][Defer] No timeout on teardown `gather` — slow hardware source cleanup could stall host exit indefinitely [host.py:291-294] — deferred, hardware not wired yet; mirror the 5.0 teardown timeout pattern when the Pi is in hand
- [x] [Review][Defer] Empty `paired_ids` with active source silently never emits, no log [sensing_ble.py:38-41] — deferred, shipped PLUGIN has source=None so loop never runs; add a warning log when real source is wired
- [x] [Review][Defer] `_run_ble` test helper uses `sleep(0.1)` instead of `_poll` — flaky on slow CI [tests/test_sensing.py:129] — deferred, polling for absence is inherently time-dependent; 100ms is reasonable for in-process stubs
- [x] [Review][Defer] `_CapturingEmit.spawn()` closes coroutine silently — if XP ever spawns, test passes with no execution [tests/test_xp_plugin.py] — deferred, XP has no spawn path today; revisit if XP gains background behavior
- [x] [Review][Defer] Seq gap on `draw` write failure — strict-monotonic display could drop next valid frame [host.py:190-201] — deferred, pre-existing concern since 7.3; display does not enforce strict-monotonic today

### Change Log

- 2026-06-19 — Story 7.4 implemented: optional physical sensing (PiSugar2 button + BLE presence), the Epic 7 capstone. Built the plugin **event-emit seam** (the `on_start` host handle gains `emit_event` validated against `manifest.emits` + `spawn` for host-owned sense loops) and the two sensing plugins, with the real hardware **gated** (`bleak`/PiSugar2 lazy `# pragma: no cover`, injected stubs — 0 new hard deps). **BLE pair-first** enforced as the security AC (unpaired devices never tracked or logged). On by default but idle without hardware (CAP-3). **Zero `core/` changes**; the face reaction (mood nudge) is split to Story 7.5. +9 tests, suite **517 pass**, 3 import contracts KEPT. Status → review.
