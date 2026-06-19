---
baseline_commit: c37d337
---
# Story 7.2: Broadcast event subscriptions

Status: done

<!-- Second Epic 7 FEATURE story. Builds directly on 7.1 (plugin contract + host + the EventKind vocabulary + the loaded.subscriptions registry). -->
<!-- HARD PREREQUISITE FOLDED IN: 7.1 review [Decision] iceboxed `plugin-host-owns-the-read-loop` — this story resolves it (AC1). -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->
<!-- Scoping decisions D1–D5 are explicit in Dev Notes. Open questions at the very end. -->

## Story

As a developer extending shelldon,
I want the pet's lifecycle moments published as broadcast `event` envelopes that the plugin-host fans out to every subscribed plugin (using the manifest-built subscription registry from 7.1),
so that behavioral plugins (the XP widget in 7.3) can react to "a message was answered" / "a day passed" off the same bus stream — without core knowing any plugin exists.

**Why this is the next Epic-7 story:** 7.1 shipped the plugin *contract* + host + the closed `EventKind` set + the `loaded.subscriptions` registry, but **nothing is routed on it yet** — a plugin's `run()` is a stay-alive stub and the host hands the *same* reader to every plugin (the 7.1 review's iceboxed single-reader limit). This story turns the registry live: it adds the `Event` wire message (AD-11's second routing mode), makes the **host own the single read loop and fan out to subscribed plugins** (resolving the 7.1 icebox), and wires **one real emitter** — core publishes `message-answered` at turn completion — to prove the path end-to-end (CAP-7: an emitted event reaches exactly the plugins that subscribed, and no others).

## Acceptance Criteria

### AC1 — The plugin-host owns the read loop and fans out to subscribed plugins (resolves the 7.1 icebox)

**Given** 7.1's `run_plugin_host` hands the *same* `reader`/`writer` to every plugin's `run(reader, writer)` — safe only for one reader (the `SINGLE-READER LIMIT` note), and the iceboxed `plugin-host-owns-the-read-loop` decision
**When** the broadcast path is built
**Then** `run_plugin_host` owns the **single** bus read loop: it reads each `Event` envelope once, looks up `loaded.subscriptions[event.event]` (the manifest-built registry from 7.1), and dispatches to each subscribed plugin — a plugin **never** calls `read_frame` on the socket itself
**And** the `Plugin` contract changes from `async def run(self, reader, writer)` to an event handler `async def on_event(self, event: Event) -> None` (the host owns I/O; the plugin reacts). `BasePlugin` keeps a no-op `on_event` default; its 7.1 read-loop `run` is removed. (This is the contract redesign 7.1 explicitly flagged as provisional — the 7.1 tests that drove `BasePlugin.run` are updated/replaced here, not preserved.)
**And** a per-plugin `on_event` that raises is isolated (logged, other subscribers still receive the event) — AD-8: a crashed plugin kills only itself, not the host or the soul.

### AC2 — A closed `Event` wire message (AD-11's second routing mode)

**Given** AD-11: "every `Envelope` has a closed header; the hub supports two routing modes both declared in `contracts/` — (1) point-to-point, (2) broadcast/subscription over a closed set of `event` kinds"
**When** the event message is added to `shelldon/contracts/__init__.py`
**Then** a new `MsgKind.EVENT` + a frozen `Event` body (tag `"event"`) carrying `event: EventKind` is added to the `Envelope.body` union and `_KIND_FOR_BODY`; an `Event` envelope is published with `dst=None` — the header field AD-11 **already reserves for broadcast** (`Envelope.dst: Actor | None`, the docstring says "`dst=None` is reserved for the broadcast/subscription mode")
**And** `MsgKind.EVENT` is deliberately **NOT** added to `ROUTING_TABLE` (that table is point-to-point mode 1, and a test enforces every *routed* point-to-point kind has an entry) — broadcast routing is a separate hub path (AC3). The `Event` body stays minimal (`event: EventKind` only; richer per-event payloads are an additive later change — D4). Whether this bumps `SCHEMA_VERSION` is D5 (recommended: no — a new additive kind).

### AC3 — The hub delivers broadcast events to the plugin-host; the host fans out to plugins

**Given** the hub (`core/bus/server.py:_route`) routes point-to-point via `ROUTING_TABLE[env.kind] -> single Actor`
**When** an `Event` envelope (`kind=EVENT`, `dst=None`) reaches the hub
**Then** `_route` takes the **broadcast branch**: it delivers the event to the plugin-host (the subscriber actor that hosts all plugins), reusing the existing `_registry`/`write_frame` mechanism, and **drops-with-a-log** if no plugin-host is connected (mirroring the existing "no connection for dest" behavior at `server.py:133-136`) — never crashing the emitter
**And** the manifest-built subscription registry lives in the **host** (`loaded.subscriptions`, built in 7.1 from plugin manifests at load, no runtime self-registration of kinds) — the hub does not need its own per-kind registry while there is exactly one plugin-host process (AD-8). The per-plugin fan-out (which plugins get which kind) is the host's job (AC1). (Hub-side registry alternative = D1.)

### AC4 — Core emits `message-answered` at turn completion (the live emitter / CAP proof)

**Given** core delivers a successful reply in `_handle_result` (`runtime.py:376-378`: `_send_reply(result.payload)` + `_push_face(FACE_REPLY)` on the `result.ok` path)
**When** an owner message is successfully answered
**Then** core publishes an `Event(event=EventKind.MESSAGE_ANSWERED)` envelope (`src=CORE`, `dst=None`, `kind=EVENT`) via `self.bus.deliver(...)` — a new emit helper alongside `_send_reply`/`_push_face` (`runtime.py:442-465`), called once per answered turn on the ok path (NOT on degrade/timeout — a degrade is not a real answer)
**And** the emit is **best-effort / fail-soft**: an event-publish failure must never break the turn loop or the slot release (same discipline as the `_record_turn`/history best-effort pattern, `runtime.py:479-484`). Core remains LLM-free and plugin-agnostic — it publishes a closed `EventKind`, knowing nothing about who (if anyone) subscribes.

### AC5 — CAP-7 success: the event reaches subscribers, only subscribers, and the boundary holds

**Given** the full path: core emits `message-answered` -> hub broadcast -> host -> `loaded.subscriptions` fan-out -> plugin `on_event`
**When** a plugin subscribing to `MESSAGE_ANSWERED` and a plugin subscribing to a *different* kind are both loaded
**Then** an end-to-end test (real `BusServer`, the in-process harness 7.1 uses) proves the subscribed plugin's `on_event` fires with the event and the non-subscribed plugin's does **not** (the manifest registry gates delivery)
**And** `uv run lint-imports` stays green — **3 contracts KEPT** (the "plugins never import core" contract still holds: plugins consume `Event` from `shelldon.contracts`, never `shelldon.core`); `uv sync --locked` 0 new deps; the full suite green at the new count (480 baseline + new − any 7.1 `run`-based tests this story legitimately replaces).

### Out of scope (explicit — do NOT do here)

- **Wiring `tool-used` and `day-alive` emitters.** They stay in the closed `EventKind` set (declared in 7.1) but have no live emitter here: `tool-used` has no source (the pet has no tools — self-coding tools are iceboxed), and `day-alive` is a scheduler concern. `message-answered` is the single CAP-proof emitter. The other two land when a source exists (likely alongside 7.3's XP needs). (D3.)
- **The XP/leveling plugin itself** (Story 7.3) — this story ships the fan-out + one emitter, not a real consuming plugin (tests use fakes).
- **A hub-side per-kind subscription registry / multi-subscriber-actor fan-out** (D1) — there is one plugin-host process (AD-8); the host owns the per-plugin registry. Build the hub-level registry only when a second subscriber *actor* (e.g. core- or display-side subscription) actually exists. YAGNI now.
- **Richer `Event` payloads** (the answered text, tool name, etc.) — `event: EventKind` only; payloads are an additive wire change when a plugin needs them (D4).
- **Plugins *emitting* events** (the `emits` manifest field) — declared in 7.1, still unconsumed. An emit path (plugin -> host -> hub -> subscribers) is a later concern; 7.2 is core-emits / plugin-consumes only.
- **Changing the turn lifecycle, the arbiter, the dispatcher (7.0), memory, or faces** — the only core touch is the additive best-effort emit helper + its one call site on the ok path.

## Tasks / Subtasks

- [x] **Task 1 — Add the `Event` wire message to `contracts/`** (AC2)
  - [x] `Event(msgspec.Struct, frozen=True, tag="event")` with `event: EventKind`; added to the `Envelope.body` union, `_KIND_FOR_BODY` (`Event: MsgKind.EVENT`), `MsgKind.EVENT = "event"`, and `__all__`. NOT added to `ROUTING_TABLE` (broadcast ≠ point-to-point).
  - [x] D5 resolved: no `SCHEMA_VERSION` bump (additive new kind).
  - [x] verify: `test_event_contract.py` round-trips an `Event` envelope (`dst=None`), asserts EVENT not in `ROUTING_TABLE`, and the closed-header guard still binds EVENT↔Event. Updated `test_every_kind_has_a_route` to exempt the broadcast kind.
- [x] **Task 2 — Host owns the read loop + the `on_event` contract** (AC1)
  - [x] `plugins/manifest.py`: `Plugin` protocol is now `async def on_event(self, event: Event)`; `BasePlugin` has a no-op `on_event` and **lost** its 7.1 `run` read-loop.
  - [x] `plugins/host.py`: `run_plugin_host` connects as `PLUGIN_HOST`, owns ONE read loop, and on each `Event` calls `_fan_out` → `_safe_on_event` per subscribed plugin (isolated try/except). Per-frame resilience mirrors transport/display (skip `ValidationError`, end on `ValueError`/`None`/EOF). `_idle` sentinel removed; SINGLE-READER-LIMIT note replaced. Added a `plugins=` injection seam for tests.
  - [x] verify: `_fan_out` delivers only to subscribers; a raising `on_event` is isolated (other subscriber still fires); the host dispatches a received Event; it skips an invalid frame and keeps dispatching; hub-EOF tears down cleanly.
- [x] **Task 3 — The hub broadcast branch** (AC3)
  - [x] `core/bus/server.py:_route`: an `EVENT` envelope takes the broadcast branch → delivers to `PLUGIN_HOST` via the extracted `_deliver_to` helper (reuses the dead-target drop/deregister). Absent host = **debug-level** drop (a no-subscriber broadcast is normal — CAP-3), not the WARNING a missing point-to-point target gets.
  - [x] verify: `test_broadcast_event_routed_to_plugin_host` (frame arrives) + `test_broadcast_event_dropped_when_no_plugin_host` (no raise).
- [x] **Task 4 — Core emits `message-answered`** (AC4)
  - [x] `core/runtime.py`: `_emit_event(kind)` helper (`bus.deliver` an `Envelope(kind=EVENT, src=CORE, dst=None, body=Event(event=kind))`, self-guarded fail-soft). Called once on the `result.ok` path in `_handle_result` (after `_record_turn`). NOT on degrade/timeout.
  - [x] verify: `test_turn_events.py` — a successful turn emits exactly one `message-answered`; a degrade emits none; a forced event-publish `OSError` doesn't break the turn (reply delivered, fence idle).
- [x] **Task 5 — End-to-end CAP-7 proof + boundary** (AC5)
  - [x] `test_message_answered_reaches_only_the_subscribed_plugin`: core turn → hub broadcast → host → the subscribed fake plugin's `on_event` fires; the unsubscribed one's does not.
  - [x] verify: `uv run lint-imports` 3 KEPT / 0 broken; `uv sync --locked` 0 new deps; full suite green.
- [x] **Task 6 — Close the icebox + final gate**
  - [x] `sprint-status.yaml`: `plugin-host-owns-the-read-loop` icebox → **done** (resolved by AC1).
  - [x] verify: `uv run pytest -q` → **493 passed**, 3 skipped; `uv run lint-imports` 3 KEPT; `uv sync --locked` 0 new deps. (Soak fix: a no-subscriber broadcast drop is debug-level, so the per-turn event no longer floods WARNING logs into tracemalloc's heap measurement.)

## Dev Notes

### Scoping decisions (made, not assumed — flagged for the owner at the end)

- **D1 — Host-side fan-out, no hub-level registry (recommended).** The manifest-built subscription registry lives in the host (`loaded.subscriptions`, built in 7.1). The hub just delivers `Event` to the one `PLUGIN_HOST` process; the host fans out per-kind to its plugins. A hub-level `EventKind -> {Actor}` registry is only needed when a *second subscriber actor* exists (e.g. core or display subscribing) — there is exactly one plugin-host (AD-8), so that is YAGNI. This also matches the 7.1 icebox note verbatim. (Open Q1.)
- **D2 — The 7.1 `Plugin.run(reader, writer)` contract is replaced by `on_event(event)`.** 7.1 explicitly marked `run` provisional; the host owning reads is the iceboxed fix. The 7.1 tests that exercised `BasePlugin.run`/the idle sentinel (`test_baseplugin_logs_on_a_framing_error`, the lifecycle tests that drove `run`) are rewritten against `on_event` + the host read loop — a contract change legitimately changes its tests (unlike 7.0's behavior-preserving rule).
- **D3 — Only `message-answered` gets a live emitter.** Clean single source (turn completion). `tool-used` has no source (no tools yet); `day-alive` is a scheduler job — both are follow-ons when a source/consumer exists (likely with 7.3). The closed `EventKind` set already declares all three (7.1), so adding emitters later is additive. (Open Q2.)
- **D4 — `Event` body is `event: EventKind` only.** The XP plugin (7.3) needs to know *that* a message was answered, not its text. A richer payload (answered text, tool name, day count) is an additive field when a consumer needs it — don't speculate now.
- **D5 — No `SCHEMA_VERSION` bump for `EVENT`.** Adding a new `MsgKind` + body variant is additive: old code never produced or consumed it, and all processes share one `contracts/` build in a deployment (same reasoning that let `proposed_ops` variants grow without a bump). Flag if the owner wants the bump for explicitness. (Open Q3.)

### The exact landing points (verified line refs)

- **Emit point** — `core/runtime.py:_handle_result`, the `result.ok` branch at `runtime.py:376-378` (`_send_reply` + `_push_face`). Add the `_emit_event(EventKind.MESSAGE_ANSWERED)` call right after the face push, inside the ok path, OUTSIDE the narrow delivery try (so a transport failure on the reply doesn't suppress the event, and an event failure doesn't suppress the slot release). Fail-soft like `_record_turn` (`runtime.py:479-484`).
- **Emit mechanism** — `self.bus.deliver(Envelope(...))`, exactly as `_send_reply` (`runtime.py:444-453`) and `_push_face` (`runtime.py:455-465`). `BusServer.deliver` (`core/bus/server.py:119-126`) is a thin wrapper over `_route`.
- **Hub routing** — `core/bus/server.py:_route` (`server.py:128-144`): today `dest = ROUTING_TABLE[env.kind]`, CORE→inbox, else `write_frame` to `_registry[dest]` with dead-target dropping. The EVENT broadcast branch slots in before the ROUTING_TABLE lookup and reuses the same `_registry`/`write_frame`/drop-on-OSError machinery.
- **Broadcast header** — `Envelope.dst: Actor | None` with `dst=None` "reserved for the broadcast/subscription mode" (`contracts/__init__.py:257` + docstring `:244-246`). 7.2 is the story that finally uses it.
- **Host registry** — `loaded.subscriptions: dict[EventKind, list[Plugin]]` built by `validate_claims` in 7.1 (`plugins/host.py`), and `run_plugin_host`'s connect/teardown shape (the `asyncio.wait(FIRST_COMPLETED)` lifecycle) — now collapses to a single host-owned read loop.
- **EventKind / PluginManifest.subscribes** — `contracts/__init__.py` (`EventKind`, added in 7.1) and `plugins/manifest.py` (`PluginManifest.subscribes`).

### The hard part: the host read loop replaces the per-plugin readers

7.1 spawned one `run(reader, writer)` task per plugin sharing the socket — the corruption risk the review caught. 7.2 inverts it: `run_plugin_host` is the **only** reader. Sketch:

```
reader, writer = await connect(socket_path, Actor.PLUGIN_HOST)
while True:
    try: env = await read_frame(reader)
    except msgspec.ValidationError: continue       # skip one bad frame
    except ValueError: break                        # framing error -> end
    if env is None: break                           # hub gone
    if env.kind is MsgKind.EVENT and isinstance(env.body, Event):
        for p in loaded.subscriptions.get(env.body.event, []):
            try: await p.on_event(env.body)
            except Exception: log.warning(...)       # AC1 isolation, per-plugin
```

No `asyncio.wait` over N plugin tasks, no `_idle` sentinel — the host's own loop IS the lifecycle. Plugins are pure reactors.

### Testing standards summary

- `uv run pytest -q` (default offline). Reuse 7.1's in-process `BusServer` harness + `sock_path` fixture (`tests/conftest.py`) and the fake-plugin pattern (`BasePlugin` subclasses overriding `on_event`, or a small recording fake). New/updated tests live in `tests/test_plugin_host.py` (+ a contracts round-trip in `tests/test_contracts_roundtrip.py`, + the emit test near the runtime/turn tests).
- This story **changes the 7.1 plugin contract**, so it WILL edit `tests/test_plugin_host.py` (the `run`/idle-sentinel tests become `on_event`/read-loop tests). That is correct — a contract change changes its tests. Keep `test_plugin_contract.py`'s manifest/vocabulary tests (still valid).
- Success = AC1–AC5 covered, full suite green, `lint-imports` 3 KEPT / 0 broken, `uv sync --locked` 0 new deps.

### Project Structure Notes

- Modified: `shelldon/contracts/__init__.py` (`MsgKind.EVENT` + `Event` body + union/`_KIND_FOR_BODY`/`__all__`), `shelldon/plugins/manifest.py` (`Plugin.on_event`, `BasePlugin`), `shelldon/plugins/host.py` (host read loop + fan-out), `shelldon/core/bus/server.py` (broadcast branch), `shelldon/core/runtime.py` (emit helper + one call site). No new module, no new dependency.
- `core/` stays LLM-free and plugin-agnostic: it emits a closed `EventKind`, never importing `shelldon.plugins`. The boundary that 7.1 made mechanical stays mechanical.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 7.2] — the epic AC: closed broadcast `event` kinds; the bus fans out to all subscribed plugins via a manifest-built registry at load; no runtime self-registration of new kinds.
- [Source: ARCHITECTURE-SPINE.md#AD-11] — closed envelope header + two routing modes; `event` kinds fanned out to N subscribers; subscription registry built at load from plugin manifests.
- [Source: ARCHITECTURE-SPINE.md#AD-8] — one plugin-host; a plugin emits/subscribes events, owns private state; a crashed plugin kills only itself.
- [Source: _bmad-output/implementation-artifacts/7-1-plugin-host-and-the-generalized-plugin-contract.md] — the contract + host + `EventKind` + `loaded.subscriptions` this story makes live; the `SINGLE-READER LIMIT` doc-note + the `plugin-host-owns-the-read-loop` icebox this story resolves (AC1).
- [Source: shelldon/core/runtime.py:364-465] — `_handle_result` ok-path (emit point) + the `_send_reply`/`_push_face` emit-helper pattern to mirror.
- [Source: shelldon/core/bus/server.py:119-144] — `deliver`/`_route`: the broadcast branch lands here.
- [Source: shelldon/contracts/__init__.py:240-281] — `Envelope` (the `dst=None` broadcast reservation), `_KIND_FOR_BODY`, `ROUTING_TABLE` (do NOT add EVENT here), `MsgKind`, `EventKind`.
- [Source: shelldon/plugins/host.py, shelldon/plugins/manifest.py] — 7.1's `run_plugin_host`/`loaded.subscriptions`/`BasePlugin` to evolve.

### Open questions for the owner (do not block dev — defaults chosen above)

1. **Fan-out location (D1):** host-side only (recommended — one plugin-host, `loaded.subscriptions` is the registry) vs build a hub-level `EventKind -> {Actor}` registry now (futureproofs a second subscriber actor, but YAGNI and couples core to a subscription wire). Pick host-side unless you foresee core/display subscribing to events soon.
2. **Emitters in 7.2 (D3):** just `message-answered` (recommended) or also wire `day-alive` now via an Epic-5 scheduler daily job (gives 7.3's XP plugin a second real event)? `tool-used` has no source regardless.
3. **SCHEMA_VERSION (D5):** leave at 1 (recommended — additive `EVENT` kind) or bump to 2 for an explicit "the wire grew" marker (forces a coordinated redeploy signal)?

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Baseline: `uv run pytest -q` → 480 passed, 3 skipped.
- Post-change gate: `uv run pytest -q` → **493 passed, 3 skipped, 5 deselected** (+13 net); `uv run lint-imports` → 3 contracts KEPT, 0 broken; `uv sync --locked` → 0 dep changes.
- Soak interaction (found + fixed mid-build): the per-turn `message-answered` event, dropped at WARNING when no plugin-host is attached, flooded pytest's captured-log buffer and tripped `test_in_process_core_does_not_accumulate`'s `tracemalloc` heap-growth assertion (passed with `-p no:logging`). Fixed at the source — a no-subscriber broadcast is debug-level (it's the normal zero-plugin steady state), not a WARNING.

### Completion Notes List

- **The 7.1 subscription registry is now live (CAP-7).** Core emits a closed `message-answered` `EventKind` at turn completion → the hub's broadcast branch delivers it to the plugin-host → the host fans out via `loaded.subscriptions` to exactly the subscribed plugins. End-to-end proven: subscriber receives it, non-subscriber doesn't.
- **Resolved the 7.1 read-loop icebox (AC1).** The host owns the **single** read loop; `Plugin.run(reader, writer)` is replaced by `on_event(event)` — plugins are pure reactors, never touching the socket. This eliminates the shared-reader corruption the 7.1 review flagged. `plugin-host-owns-the-read-loop` icebox → done.
- **AD-11 two routing modes, cleanly split.** Point-to-point stays `ROUTING_TABLE`-driven; `EVENT` (broadcast, `dst=None`) takes a dedicated hub branch. Extracted `_deliver_to` so both paths share the dead-target drop/deregister. `EVENT` is deliberately NOT in `ROUTING_TABLE` (the completeness test now exempts it).
- **Core stays plugin-agnostic + LLM-free.** `_emit_event` publishes a closed `EventKind` knowing nothing about subscribers; best-effort (a publish failure can't break the turn or slot release, like `_record_turn`). Import-linter still 3 KEPT — `core/` never imports `shelldon.plugins`.
- **Severity correctness (the soak fix).** A broadcast with no plugin-host subscribed is the **normal** zero-plugin steady state (CAP-3 optionality), so it's a debug drop — unlike a missing point-to-point transport/display, which stays WARNING. This is a correctness fix, not a test accommodation.
- **Contract change → 7.1 tests legitimately updated** (flagged in the story): `test_plugin_host.py`'s `run`/idle-sentinel tests became `on_event`/read-loop tests; `test_plugin_contract.py`'s "no `MsgKind.EVENT`" assertion (a 7.1-scope invariant) became "EventKind stays distinct from MsgKind." No behavior-preserving rule applies — the contract evolved by design.
- **Scope held (D3):** only `message-answered` has a live emitter. `tool-used` (no source — no tools) and `day-alive` (scheduler) stay declared-but-unemitted; their emitters land when a source/consumer exists (likely 7.3).
- **Open questions Q1–Q3 left at defaults** (host-side fan-out, message-answered only, no schema bump) — owner ran dev without overriding.

### File List

- `shelldon/contracts/__init__.py` — MODIFIED. `MsgKind.EVENT`, `Event` body, added to the `Envelope.body` union + `_KIND_FOR_BODY` + `__all__`. No `SCHEMA_VERSION` bump, no `ROUTING_TABLE` row.
- `shelldon/plugins/manifest.py` — MODIFIED. `Plugin.on_event` replaces `run`; `BasePlugin` no-op `on_event` (read-loop removed); dropped the now-unused logging import.
- `shelldon/plugins/host.py` — MODIFIED. `run_plugin_host` owns the single read loop + `_fan_out`/`_safe_on_event`; `plugins=` injection seam; `_idle` sentinel + SINGLE-READER-LIMIT note removed.
- `shelldon/core/bus/server.py` — MODIFIED. `_route` broadcast branch for `EVENT`; extracted `_deliver_to` (shared dead-target handling); debug-level no-subscriber drop.
- `shelldon/core/runtime.py` — MODIFIED. `_emit_event` helper + the `message-answered` emit on the `result.ok` path of `_handle_result` (review D1: placed AFTER `arbiter.complete()` so a suspended emit can't hold the turn slot).
- `shelldon/plugins/manifest.py`, `shelldon/plugins/host.py` — review D2: `on_event` fast/non-blocking doc-contract on `Plugin.on_event` + `_fan_out`.
- `tests/test_turn_events.py` — review D1: +`test_slot_is_released_even_if_the_event_emit_suspends` (proves slot release precedes the emit).
- `tests/test_event_contract.py` — NEW. 4 tests: Event round-trip, kind↔body guard, not-in-ROUTING_TABLE, typo'd-kind decode.
- `tests/test_turn_events.py` — NEW. 4 tests: ok-turn emits message-answered, degrade emits none, publish-failure-doesn't-break-turn, end-to-end subscriber-only delivery.
- `tests/test_plugin_host.py` — MODIFIED. `run`/idle-sentinel tests → `_fan_out` + host read-loop dispatch + isolation + invalid-frame-resilience tests.
- `tests/test_plugin_contract.py` — MODIFIED. The 7.1 "no `MsgKind.EVENT`" assertion → "EventKind distinct from MsgKind" (7.2-correct).
- `tests/test_bus_routing.py` — MODIFIED. +2: broadcast-event-routed-to-plugin-host, dropped-when-no-host.
- `tests/test_contracts_roundtrip.py` — MODIFIED. `test_every_kind_has_a_route` exempts the broadcast EVENT kind.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `7-2 → in-progress → review`; icebox `plugin-host-owns-the-read-loop → done`.

### Review Findings

<!-- RESOLVED 2026-06-19: both [Decision]s fixed (owner-recommended options). Decision 1 = Option A (move emit after arbiter.complete() + regression test test_slot_is_released_even_if_the_event_emit_suspends). Decision 2 = Option A (on_event fast/non-blocking doc-contract on Plugin.on_event + _fan_out; timeout deferred to 7.3). Suite 494 pass. The 3 [Defer]s are pre-existing-pattern (whole-bus-client class), iceboxed as `bus-client-oserror-and-drain-resilience`. -->

- [x] [Review][Decision] `_emit_event` placement: turn slot can be held if `drain()` suspends — `_emit_event` is called before `_await_reap`/`arbiter.complete()`; if `bus.deliver`→`write_frame`→`drain()` suspends (plugin-host alive but not draining its read buffer), the `except Exception` guard never fires, `_await_reap` is never reached, and the turn slot is held forever — violating the "fail-soft / slot-release always completes" spec guarantee. Options: **(A) move `_emit_event` after `arbiter.complete()`** (recommended — matches `_record_turn` placement, zero new parameters, slot always released before event fires, subscribers see MESSAGE_ANSWERED after reap rather than during it); (B) wrap `bus.deliver` in `asyncio.wait_for(timeout=N)` inside `_emit_event` (keeps pre-reap timing, need to pick N); (C) fire-and-forget via `asyncio.create_task` (fully decoupled but task floats and can race with a quick subsequent turn). [core/runtime.py:388]
- [x] [Review][Decision] Sequential `_fan_out` with no per-plugin timeout — a slow or I/O-blocking plugin `on_event` delays all subsequent subscribers; if the host read loop is pinned long enough, the hub's kernel buffer for PLUGIN_HOST fills and core's next `_emit_event`→`drain()` suspends (cascades to Decision 1 scenario). Zero real plugins in 7.2 makes this moot today; becomes load-bearing in 7.3. Options: **(A) accept sequential, add docstring constraint that `on_event` must be fast/non-blocking** (recommended — no real subscribers yet, premature to pick a timeout without a real workload); (B) add per-plugin `asyncio.wait_for` timeout in `_safe_on_event`; (C) concurrent fan-out via `asyncio.gather` (still blocks read loop until all complete; adds complexity). [plugins/host.py:144]
- [x] [Review][Defer] `OSError` from `read_frame` not caught in `run_plugin_host` read loop — an abrupt socket reset (ECONNRESET) propagates through `read_frame` as `OSError` (not `ValueError`/`ValidationError`), escapes the while loop, and exits `run_plugin_host` with a non-None exception on `host_task`. Pre-existing pattern (same gap existed in 7.1's `BasePlugin.run`). Fix: add `except OSError` branch that logs and returns. [plugins/host.py:179]
- [x] [Review][Defer] `_envelopes()` round-trip fixture missing `Event` body — the M0/AD-10 canonical `_envelopes()` fixture in `test_contracts_roundtrip.py` does not include an `Event`-body envelope. Functionally covered by `test_event_contract.py`; low risk. Add an `Event` case to the fixture for completeness. [tests/test_contracts_roundtrip.py]
- [x] [Review][Defer] `ROUTING_TABLE[env.kind]` KeyError for future unknown non-EVENT kinds in `_route` — a new MsgKind added without a ROUTING_TABLE entry (and not EVENT) would hit an unguarded `KeyError` in `_route`, killing the sender's connection handler. Pre-existing gap; not introduced by this PR. [core/bus/server.py:143]

### Change Log

- 2026-06-19 — Story 7.2 implemented: broadcast event subscriptions. Added the `Event` wire message (`MsgKind.EVENT` + `Event` body, AD-11 mode 2, `dst=None`); made the plugin-host own the single read loop and fan out via `loaded.subscriptions` (resolving the 7.1 `plugin-host-owns-the-read-loop` icebox, `Plugin.run`→`on_event`); added the hub broadcast branch (`_route`/`_deliver_to`); core emits `message-answered` at turn completion (fail-soft, plugin-agnostic). CAP-7 proven end-to-end (event reaches subscribers only). Soak severity fix: a no-subscriber broadcast drop is debug-level. +13 net tests, suite **493 pass**, 3 import contracts KEPT, 0 new deps. Status → review.
- 2026-06-19 — Code review addressed (2 Decisions resolved, 3 deferred). **D1 (real bug I introduced):** `_emit_event` ran before `arbiter.complete()`, so a backpressured-plugin-host `drain()` suspend would hold the turn slot forever — moved the emit AFTER slot release (+regression test). **D2:** sequential `_fan_out` has no per-plugin timeout — added an `on_event` fast/non-blocking doc-contract; timeout deferred to 7.3. The 3 [Defer]s (read_frame OSError escape; unbounded consumer-drain backpressure across all emit helpers; round-trip-fixture/`_route`-KeyError nits) are a pre-existing whole-bus-client class — iceboxed as `bus-client-oserror-and-drain-resilience`. +1 test, suite **494 pass**, 3 import contracts KEPT, 0 new deps. Status → done.
