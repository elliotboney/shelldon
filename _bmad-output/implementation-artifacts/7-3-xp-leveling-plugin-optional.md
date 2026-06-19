---
baseline_commit: eb25937
---
# Story 7.3: XP / leveling plugin (optional)

Status: done

<!-- Third Epic 7 FEATURE story — the FIRST real plugin. Validates the whole 7.1 (contract+host) + 7.2 (events) stack end-to-end with a real consumer. -->
<!-- Introduces the plugin DRAW seam (plugins can consume events since 7.2, but cannot yet emit/draw — the host owns the writer). -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->
<!-- Scoping decisions D1–D5 are explicit in Dev Notes. Open questions at the very end. -->

## Story

As the owner,
I want an optional XP/leveling plugin that earns XP from the pet's lifecycle events and draws a status-bar widget,
so that I get the gamified "it's growing" feel — built **entirely as a plugin**, with `core/` unchanged and the import-linter still green.

**Why this is the next Epic-7 story:** 7.1 shipped the plugin contract + host; 7.2 made events flow (core emits `message-answered` → host fans out to subscribers). But a plugin can still only *consume* — it has no way to *draw*, because the host owns the single bus connection (reader **and** writer). The XP widget is the forcing function for the **plugin draw seam**: the host hands each plugin a region-scoped emitter so it can push a `StateSnapshot` to its **claimed** display region (AD-5: plugins claim widget regions; core owns `face`). The XP plugin then proves CAP-7's whole point — a real behavioral capability added with **zero `core/` changes** and the LLM-free-core import contract intact.

## Acceptance Criteria

### AC1 — An XP plugin module, discovered like any plugin, with zero core changes

**Given** the host discovers plugins as modules exposing `MANIFEST` (7.1)
**When** `shelldon/plugins/xp.py` is added
**Then** it exposes `MANIFEST: PluginManifest` declaring `subscribes=(MESSAGE_ANSWERED, TOOL_USED, DAY_ALIVE)` and `regions=(Region.STATUS_BAR,)`, plus a `PLUGIN` instance (an `XpPlugin`), and is discovered + loaded by the unchanged `run_plugin_host`
**And** adding or removing `xp.py` touches **no file under `shelldon/core/`** — the capability is entirely a plugin (the epic's "full v1 XP parity, zero core changes" bar). `uv run lint-imports` stays green: `xp.py` imports only `shelldon.contracts` + `shelldon.plugins.manifest`, never `shelldon.core`.

### AC2 — The plugin owns its private XP/level state (never core's soul)

**Given** AD-8: a plugin owns PRIVATE state, never core's memory/soul
**When** the XP plugin updates XP
**Then** it persists XP + level to its **own** store (default `~/.shelldon/plugins/xp/state.json`, injectable for tests), written atomically (the `tmp → fsync → os.replace` idiom, cf. `core/state.py:136-142`) so a crash mid-write leaves the prior good file
**And** it reads no core state and writes nothing under core's `memory/`, `state.json`, `faces.toml`, or `history.db` — its store is isolated under a plugin-owned dir. On load it restores prior XP (the pet "keeps growing" across restarts).

### AC3 — The plugin draw seam: the host hands each plugin a region-scoped emitter

**Given** the host owns the single bus connection (7.2); a plugin must not write the socket itself
**When** `run_plugin_host` connects and binds plugins
**Then** the `Plugin` contract gains `async def on_start(self, emit) -> None` (host calls it once after connect, before the read loop; `BasePlugin` default stores `emit` and no-ops), where `emit` is a **region-scoped** sender `async def emit(region: Region, face: str)` the host builds per-plugin
**And** the host's `emit` closure **validates `region in plugin.manifest.regions`** (runtime single-writer guard — a plugin can only draw what it claimed at load; an unclaimed region is logged + dropped), then sends `StateSnapshot(region, seq, face)` to the display (`Envelope(kind=STATE_SNAPSHOT, src=PLUGIN_HOST, dst=DISPLAY)`) on the host's writer, with a **host-managed per-region monotonic `seq`** — so the display's latest-wins compositor (`display/service.py:55-58`, region-keyed) renders it with **no display change**
**And** `validate_claims` rejects a plugin claiming `Region.FACE` (core owns the face region, AD-5) — a load-time `PluginLoadError` (closes a 7.1 gap: plugins must not claim the core-owned region).

### AC4 — XP rules: events earn XP; level is derived; the widget redraws

**Given** the XP plugin subscribes to the three lifecycle kinds
**When** a `message-answered` event arrives (the one live emitter today — 7.2 D3)
**Then** the plugin awards XP (default **+10**), recomputes level (default **level = 1 + xp // 100**), persists (AC2), and **draws** the status-bar widget to `Region.STATUS_BAR` via the bound `emit` (default text e.g. `"Lv{level} · {xp} XP"`)
**And** it also draws its current state once on `on_start` (so the widget shows on boot, not only after the first event), and it subscribes to `tool-used`/`day-alive` for **v1 parity** (their award rules exist) even though those have **no emitter yet** (7.2 D3) — harmless: the registry simply never delivers an unemitted kind. (D3/D4.)

### AC5 — CAP-7 end-to-end + the boundary holds

**Given** the full stack: core emits `message-answered` → hub broadcast → host → `XpPlugin.on_event` → XP++ → `emit` → `StateSnapshot` → display
**When** an end-to-end test runs a successful turn with the XP plugin loaded (real `BusServer` + a recording display)
**Then** the display receives a `STATUS_BAR` snapshot whose text reflects the incremented XP, and the plugin's persisted state shows the new XP/level
**And** adding the plugin leaves `core/` byte-unchanged and `uv run lint-imports` **3 contracts KEPT**; `uv sync --locked` 0 new deps; full suite green.

### Out of scope (explicit — do NOT do here)

- **Wiring `tool-used` / `day-alive` emitters** — still no source (7.2 D3). The XP plugin *subscribes* and has award rules ready, but only `message-answered` actually fires today. Don't add core emitters here.
- **Real E-Ink widget layout / fonts / partial-refresh** — the widget is a generic string in `StateSnapshot.face`; the stub renderer records it. Real panel layout of the status-bar region is the hardware bring-up (deferred, like the face bitmaps). No display-service change.
- **Plugins emitting bus *events*** (the `emits` manifest field) — the draw seam is plugin→display `StateSnapshot` only. A general plugin event-emit path is a later concern.
- **A new contract / `MsgKind` / `Region` member / widget body type** — reuse `StateSnapshot` + `Region.STATUS_BAR` (D5). No `SCHEMA_VERSION` touch.
- **Touching the turn lifecycle, the per-plugin timeout (7.2 D2 deferred), or the bus-client resilience icebox** — out of scope.

## Tasks / Subtasks

- [x] **Task 1 — The draw seam: `on_start(emit)` + the host's region-scoped emitter** (AC3)
  - [x] `plugins/manifest.py`: added `async def on_start(self, emit)` to `Plugin`; `BasePlugin` stores `self._emit = emit` (no-op default). `on_event` unchanged.
  - [x] `plugins/host.py`: `_make_emitter(plugin, writer, seqs)` returns `emit(region, face)` that guards `region in manifest.regions` (else log+drop), bumps a host-managed per-region `seq`, and `write_frame`s a `StateSnapshot` (src=PLUGIN_HOST, dst=DISPLAY). `run_plugin_host` binds each plugin via `_safe_on_start` (isolated) before the read loop.
  - [x] `plugins/host.py`: `validate_claims` rejects `Region.FACE` (core-owned, AD-5 — `PluginLoadError`).
  - [x] verify: draw-to-claimed-region reaches DISPLAY; unclaimed-region emit dropped+logged; FACE claim → `PluginLoadError` (3 tests in `test_plugin_host.py`).
- [x] **Task 2 — Private XP state store** (AC2)
  - [x] `XpState(xp, level)` (msgspec.Struct) + `_load_state`/`_save_state` (atomic `tmp→fsync→os.replace`, mirrors `core/state.py`); fresh/corrupt → default `xp=0, level=1`.
  - [x] verify: persists + reloads; fresh dir defaults; atomic (no temp left); corrupt file falls back to default (4 tests).
- [x] **Task 3 — `XpPlugin` + MANIFEST** (AC1, AC4)
  - [x] `shelldon/plugins/xp.py`: `MANIFEST` (3 kinds, claims `STATUS_BAR`), `XpPlugin(BasePlugin)` — `on_start` loads state (lazily — no import-time IO) + draws; `on_event` awards (`+10`/msg via `_AWARDS`), recomputes `level=1+xp//100`, persists, redraws `"Lv{level} · {xp} XP"`. `PLUGIN = make_xp_plugin()`.
  - [x] verify: `message-answered`×10 → xp=100, level=2, persisted, redraw; `on_start` draws restored state; manifest subscribes 3 + claims STATUS_BAR (4 tests).
- [x] **Task 4 — End-to-end CAP-7 + the zero-core-change proof** (AC1, AC5)
  - [x] e2e (real `BusServer` + DISPLAY connection): a successful core turn → `message-answered` → XP plugin draws `"Lv1 · 10 XP"` to STATUS_BAR + persists xp=10 (filtering core's interleaved FACE pushes).
  - [x] verify: **`git status -- shelldon/core/` empty** (zero core changes); `uv run lint-imports` 3 KEPT / 0 broken; `uv sync --locked` 0 new deps; full suite **505 pass**.

## Dev Notes

### Scoping decisions (made, not assumed — flagged for the owner at the end)

- **D1 — Draw seam = `on_start(self, emit)` binding a persistent region-scoped emitter (recommended)** over passing `emit` into every `on_event`. Reasons: the widget should draw on boot (not only after the first event); it keeps `on_event(event)`'s 7.2 signature stable; the emitter is a held capability. `on_start` is additive (BasePlugin default no-ops). (Open Q1.)
- **D2 — XP/level defaults:** `message-answered = +10 XP`; `level = 1 + xp // 100`; widget text `"Lv{level} · {xp} XP"`. Concrete and tweakable; the epic doesn't specify numbers. (Open Q2.)
- **D3 — Subscribe to all three kinds now (v1 parity).** `tool-used`/`day-alive` have award rules but no emitter (7.2 D3) — subscribing is harmless (the registry never delivers an unemitted kind) and means the plugin is complete the moment those emitters land.
- **D4 — The widget is a generic string in `StateSnapshot.face`.** Reusing `StateSnapshot` (region + seq + a render string) avoids a new contract/body type AND any display-service change (the compositor is region-generic). `face` is a slight misnomer for a widget, but it's "the thing to render in this region." A dedicated widget body is over-engineering for v1. (Open Q3.)
- **D5 — Private state = JSON at `~/.shelldon/plugins/xp/state.json` (injectable).** A plugin owns its own store under a plugin-scoped dir; JSON is enough for `{xp, level}`. Atomic write mirrors `core/state.py`.

### The draw seam (the one genuinely new mechanism)

Today the host's connection is read-only in practice (the 7.2 read loop). 7.3 adds writes on the SAME connection — safe because the host's own task does both sequentially (read loop → `on_event` → `emit` → `write_frame`; no concurrent writers). The emitter is **region-scoped per plugin**: the host builds it knowing that plugin's claimed regions, so the runtime guard (`region in manifest.regions`) plus the load-time conflict check (7.1) plus the new FACE-rejection give the AD-5 "no two writers per region, core owns face" guarantee at both load and runtime.

The widget `StateSnapshot` routes through the EXISTING table (`STATE_SNAPSHOT → DISPLAY`, `contracts:280`) — the hub routes by `kind`, ignoring `src`, so `src=PLUGIN_HOST` is fine. The display composites by `Region` (`service.py:55-58`): `FACE` (core's stream) and `STATUS_BAR` (the plugin's stream) are independent latest-wins slots — the host manages a separate monotonic `seq` for the plugin's region, never colliding with core's FACE seq.

### Verified seams (line refs)

- `StateSnapshot(region, seq, face)` — `contracts/__init__.py:212-225`; `Region.STATUS_BAR` — added in 7.1; `ROUTING_TABLE[STATE_SNAPSHOT] = DISPLAY` — `contracts:280`.
- Host owns reader+writer; `on_event` contract — `plugins/host.py` `run_plugin_host` + `plugins/manifest.py`. `validate_claims` (region/resource conflict) — `plugins/host.py`.
- Display latest-wins per region (renders any region, no FACE special-casing) — `display/service.py:30-78`; `StubRenderer` records snapshots — `display/renderer.py`.
- Atomic write idiom (`mkstemp` → write → `os.fsync` → `os.replace`) — `core/state.py:136-142`.
- `write_frame` — `core/bus/frame.py:53-55` (the host uses it; the plugin never imports it).
- The import boundary: `pyproject.toml` "plugins never import core (AD-8/CAP-7)" — `xp.py` must stay on `shelldon.contracts` + `shelldon.plugins.manifest`.

### Testing standards summary

- `uv run pytest -q` (offline). Reuse the in-process `BusServer` harness + `sock_path` fixture; a recording display = `run_display(sock, StubRenderer())` or read frames off a DISPLAY connection directly. Inject the XP state path via `tmp_path` (never write real `$HOME`, per conftest discipline). Inject plugins via `run_plugin_host(..., plugins=[XpPlugin(state_path=...)])`.
- Success = AC1–AC5 covered; **no `shelldon/core/` file modified** (the headline proof); `lint-imports` 3 KEPT; `uv sync --locked` 0 new deps; full suite green.

### Project Structure Notes

- New: `shelldon/plugins/xp.py` (MANIFEST + `XpPlugin` + `XpState`/persistence; or split state into a tiny helper). Modified: `shelldon/plugins/manifest.py` (`on_start` + `BasePlugin._emit`), `shelldon/plugins/host.py` (`_make_emitter`, `on_start` bind, FACE rejection). **No `shelldon/core/` change.** No new dependency (stdlib `json`/`os`/`tempfile`).
- Production wiring: `xp.py` lives in the `shelldon.plugins` package, so the existing `run_plugin_host` discovers it automatically — meaning once merged it is ON by default. If the owner wants it opt-in, that's a discovery-filter decision (Open Q4).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 7.3] — XP plugin: subscribed events update private XP/level + draw a status-bar widget in a claimed region; add/remove leaves `core/` unchanged + import-linter green (full v1 XP parity, zero core changes).
- [Source: ARCHITECTURE-SPINE.md#AD-8] — one plugin model; plugins own private state, claim display regions, never import core; a crashed plugin kills only itself.
- [Source: ARCHITECTURE-SPINE.md#AD-5 / #CAP-9] — display compositor; core owns the `face` region, plugins claim widget regions; no two writers per region; region ids closed in `contracts/`.
- [Source: _bmad-output/implementation-artifacts/7-1-…contract.md, 7-2-…subscriptions.md] — the contract+host (7.1) and event flow (7.2) this story consumes; `Plugin.on_event`, `loaded.subscriptions`, `validate_claims`.
- [Source: shelldon/contracts/__init__.py:212-225,280] — `StateSnapshot`, `Region.STATUS_BAR`, the `STATE_SNAPSHOT → DISPLAY` route.
- [Source: shelldon/display/service.py:30-78] — region-keyed latest-wins compositor (renders the widget region with no change).
- [Source: shelldon/core/state.py:129-142] — the atomic-write idiom for the plugin's private store.

### Open questions for the owner (do not block dev — defaults chosen above)

1. **Draw seam (D1):** `on_start(emit)` persistent binding (recommended) vs `on_event(event, emit)` per-call? The former allows a boot-time draw.
2. **XP numbers (D2):** +10 / message, level = 1 + xp//100, text "Lv{level} · {xp} XP" — fine, or do you want a curve (e.g. rising per-level thresholds) and per-kind awards (message vs tool vs day)?
3. **Widget payload (D4):** reuse `StateSnapshot.face` as a generic string (recommended), or add a dedicated widget body now (contract + display change)?
4. **On by default?** Living in `shelldon.plugins`, the XP plugin is auto-discovered → ON once merged. Keep it on (it's the showcase), or add an opt-in discovery filter (env var / config) so the pet ships plugin-free by default?

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Baseline: `uv run pytest -q` → 494 passed, 3 skipped.
- Post-change gate: `uv run pytest -q` → **505 passed, 3 skipped, 5 deselected** (+11); `uv run lint-imports` → 3 contracts KEPT, 0 broken; `uv sync --locked` → 0 dep changes; `git status -- shelldon/core/` → **empty (zero core changes)**.

### Completion Notes List

- **CAP-7's whole point, proven: a real behavioral capability with ZERO `core/` changes.** The XP plugin lives entirely in `shelldon/plugins/xp.py` + the draw-seam additions to `plugins/` — `git status -- shelldon/core/` is empty, and `lint-imports` stays 3 KEPT (`xp.py` imports only `shelldon.contracts` + `shelldon.plugins.manifest`).
- **The draw seam (the one new mechanism).** The host owns the single bus connection; it now hands each plugin a **region-scoped** `emit(region, face)` via `on_start(emit)`. The emitter guards `region ∈ manifest.regions` (runtime single-writer) and sends a `StateSnapshot` to the display with a host-managed per-region `seq`. Reuses the existing `STATE_SNAPSHOT → DISPLAY` route + the region-generic compositor — **no contract or display change** (D4/D5).
- **Closed a 7.1 gap:** `validate_claims` now rejects a plugin claiming `Region.FACE` (core owns it, AD-5) — load-time `PluginLoadError`. Belt-and-suspenders with the runtime emit guard.
- **Private state, isolated + atomic.** `XpState` persists to the plugin's own dir (`~/.shelldon/plugins/xp/state.json`, injectable) via `tmp→fsync→os.replace`; load-or-default on a fresh/corrupt file. Never touches core's memory/state/faces/history.
- **On by default (Q4 default).** The XP plugin auto-discovers from `shelldon.plugins`, so it's live once merged. To keep that safe, state-load is **lazy** (no filesystem IO at import — the module-level `PLUGIN` resolves `DEFAULT_XP_STATE_PATH` lazily), and `conftest` redirects that global so the app-smoke turn never writes real `$HOME`. Updated the 7.1 "discovers nothing" test → "discovers the xp plugin."
- **XP rules (D2 defaults):** `+10`/message-answered, `level = 1 + xp//100`, widget `"Lv{level} · {xp} XP"`. Subscribes to `tool-used`/`day-alive` for v1 parity (award rules wired) though neither has an emitter yet (7.2 D3) — harmless.
- **Open questions Q1–Q4 left at defaults** (on_start binding, the XP numbers, StateSnapshot reuse, on-by-default) — owner ran dev without overriding.

### File List

- `shelldon/plugins/xp.py` — NEW. `XpState` + `_load_state`/`_save_state` (atomic), `XpPlugin` (lazy-loading, on_start draw + on_event award/redraw), `MANIFEST`, `make_xp_plugin`, `PLUGIN`.
- `shelldon/plugins/manifest.py` — MODIFIED. `Plugin.on_start(emit)` + `BasePlugin._emit` store.
- `shelldon/plugins/host.py` — MODIFIED. `_make_emitter` (region-scoped draw seam), `_safe_on_start` (isolated bind), `on_start` binding loop in `run_plugin_host`; `validate_claims` FACE rejection.
- `tests/test_xp_plugin.py` — NEW. 8 tests: private state (default/persist/atomic/corrupt), XP rules + draw + manifest, end-to-end CAP-7.
- `tests/test_plugin_host.py` — MODIFIED. +3 draw-seam tests (claimed draw, unclaimed drop, FACE rejection); the 7.1 "discovers nothing" test → "discovers the xp plugin."
- `tests/conftest.py` — MODIFIED. Redirect `shelldon.plugins.xp.DEFAULT_XP_STATE_PATH` to a tmp file (the auto-on XP plugin must not write real `$HOME`).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `7-3 → in-progress → review`.

### Review Findings

<!-- RESOLVED 2026-06-19: all 3 Decisions + 7 Patches fixed; 3 [Defer]s left deferred. Decisions (recommended options): D1 — accept the synchronous fsync (one per owner message; documented in on_event). D2 — DROPPED the derived `level` field; XpState stores only `xp`, `level` is a computed property (can't drift; +test_level_is_never_persisted). D3 — accept graceful degradation, documented in _safe_on_start (the OSError patch removes the realistic raise-from-load case). Patches: real `Emit` TypeAlias (Callable/Awaitable imported, on_start annotated); _load_state catches OSError + logs (+test); _save_state failure logged + redraw still fires (+test); seq committed only after a successful write_frame; test _read_status_bar isinstance-guarded + shares the outer deadline; e2e finally swallows Cancelled/Timeout so it can't mask an assertion. +3 tests, suite 508 pass, 3 contracts KEPT, core/ still byte-unchanged. -->

- [x] [Review][Decision] `_save_state` is synchronous blocking I/O inside async `on_event` — every XP earn calls `_save_state` (which does `fsync`) directly; this blocks the entire asyncio event loop for the duration of the syscall (potentially tens of ms under I/O pressure). Options: **(A) accept as-is and document that `on_event` must not be called at high frequency** (recommended — this is a pet bot; one fsync per owner message is fine, and the alternative adds non-trivial complexity); (B) wrap `_save_state` in `asyncio.to_thread`. [shelldon/plugins/xp.py:120]
- [x] [Review][Decision] `level` persisted as a derived field with no on-load consistency check — `XpState.level` is always `1 + xp // 100` yet it's stored in JSON; a hand-edited or partially-written JSON could store `xp=150, level=1`, and on reload the displayed widget will show the wrong level until the next `on_event` fires. Options: **(A) drop `level` from `XpState` — compute it always from `xp` via `_level_for`** (recommended — removes the derived-field inconsistency entirely, the field is redundant); (B) add a `level = _level_for(state.xp)` recompute step in `_load_state` after decoding; (C) accept as-is (only affects manual edits). [shelldon/plugins/xp.py:XpState / _load_state]
- [x] [Review][Decision] `_safe_on_start` isolates failures but leaves the plugin in event-dispatch loop with default zero state — if `on_start` raises after `super().on_start(emit)` stores `self._emit` (e.g., `_load_state` hits an unhandled `OSError`), the plugin has an emitter but `self.state = XpState()` (zero XP placeholder), so every subsequent `on_event` earns XP from zero regardless of prior history. The plugin is "isolated" in name but continues to receive events and draw misleading state. Options: **(A) accept as graceful degradation — document it** (recommended if fixing P2 handles the OSError case); (B) add an `_disabled` flag to `BasePlugin`, set it in `_safe_on_start` on failure, and skip disabled plugins in `_fan_out`. [shelldon/plugins/host.py:_safe_on_start]
- [x] [Review][Patch] `Emit` type alias is a dead string literal — `Emit = "Callable[[Region, str], Awaitable[None]]"` is not a live type; `Callable`/`Awaitable` are not imported so the alias cannot be used in annotations and mypy/pyright ignores it. Fix: `from collections.abc import Callable, Awaitable` + `Emit: TypeAlias = Callable[[Region, str], Awaitable[None]]`. [shelldon/plugins/manifest.py]
- [x] [Review][Patch] `_load_state` silently drops `PermissionError` / `IsADirectoryError` — catches only `(FileNotFoundError, msgspec.DecodeError, msgspec.ValidationError)`; a permission error or directory-at-path propagates through `_load_state`, out of `XpPlugin.on_start`, and is swallowed by `_safe_on_start` with no operator-visible log. Fix: catch `OSError` broadly and log a warning before returning `XpState()`. [shelldon/plugins/xp.py:68]
- [x] [Review][Patch] `_save_state` raises → `_draw()` never called — when disk is full or permissions fail, `_save_state` re-raises `OSError` before `_draw()` is reached; the in-memory XP has advanced but the widget stays stale at pre-event XP. Fix: wrap `_save_state` in try/except, log the save failure, then unconditionally call `_draw()`. [shelldon/plugins/xp.py:117-120]
- [x] [Review][Patch] `seqs[region]` incremented before `write_frame` succeeds — on `OSError` in `write_frame`, the per-region seq counter is permanently skipped; any future strict-monotonic-gap semantics in the display compositor would silently drop the next valid frame. Fix: assign `seqs[region]` only after `write_frame` returns without raising. [shelldon/plugins/host.py:_make_emitter]
- [x] [Review][Patch] `_read_status_bar` missing `isinstance(env.body, StateSnapshot)` guard — `env.body.region` AttributeError crashes the helper with a misleading failure if any non-snapshot envelope reaches the display reader. Fix: `if env is not None and isinstance(env.body, StateSnapshot) and env.body.region is Region.STATUS_BAR:`. [tests/test_xp_plugin.py:109]
- [x] [Review][Patch] `_read_status_bar` inner `wait_for` ignores outer deadline — each frame read uses a fresh `timeout=1.0s`; if many FACE frames arrive before a STATUS_BAR frame, the helper can run up to N×1.0s past its declared deadline. Fix: `remaining = max(0.01, deadline - loop.time()); await asyncio.wait_for(read_frame(reader), timeout=remaining)`. [tests/test_xp_plugin.py:107]
- [x] [Review][Patch] e2e test `finally` block raises `CancelledError` from `wait_for(host_task)` — if the host task was already cancelled during teardown, `wait_for` re-raises `CancelledError`, which unwinds the `finally` and masks any prior `AssertionError`. Fix: `try: await asyncio.wait_for(host_task, timeout=1.0); except (asyncio.CancelledError, asyncio.TimeoutError): pass`. [tests/test_xp_plugin.py:149]
- [x] [Review][Defer] `seqs` dict RMW non-atomic — `seqs[region] = seqs.get(region, 0) + 1` is safe under CPython's GIL today (no await between read and write), but fragile if the `_safe_on_start` startup loop is ever refactored to use `asyncio.gather`. Pre-existing pattern; annotate the assumption. [shelldon/plugins/host.py:_make_emitter]
- [x] [Review][Defer] `tempfile.mkstemp` prefix uses `path.name` — `prefix=path.name + "."` would fail with special chars if `DEFAULT_XP_STATE_PATH` is ever reconfigured. Low risk with the hardcoded `state.json` path; use a static prefix like `.xp-tmp-` for robustness. [shelldon/plugins/xp.py:85]
- [x] [Review][Defer] `_draw` in `on_start` fires before display socket confirmed ready — a transient `write_frame` OSError on the initial draw propagates through `on_start` and is caught by `_safe_on_start`; widget is silently disabled for the session with no retry. Document as a startup-ordering constraint (DISPLAY must be up before the plugin-host connects). [shelldon/plugins/host.py:_safe_on_start]

### Change Log

- 2026-06-19 — Story 7.3 implemented: the XP/leveling plugin — the first REAL plugin, proving CAP-7 (a behavioral capability with **zero `core/` changes**, import-linter 3 KEPT). Added the plugin **draw seam** (`Plugin.on_start(emit)` + the host's region-scoped emitter → `StateSnapshot` to a claimed widget region, reusing the existing display route/compositor — no contract/display change); `validate_claims` now rejects a plugin claiming the core-owned FACE region (closes a 7.1 gap). The XP plugin earns XP from `message-answered`, owns its private atomic JSON state, and redraws a status-bar widget; it's on by default (lazy state-load so import is IO-free; `conftest` isolates the path). +11 tests, suite **505 pass**, 3 import contracts KEPT, 0 new deps. Status → review.
- 2026-06-19 — Code review addressed (3 Decisions + 7 Patches; 3 deferred). Key change: **dropped the derived `level` field** — `XpState` persists only `xp`, `level` is a computed property (can't drift). Hardening: `_load_state` catches `OSError` + logs; a save failure logs + still redraws; the draw `seq` commits only after a successful write; real `Emit` `TypeAlias`; 3 test-robustness patches (isinstance guard, shared deadline, teardown can't mask assertions). +3 tests, suite **508 pass**, 3 import contracts KEPT, `core/` still byte-unchanged. Status → done.
