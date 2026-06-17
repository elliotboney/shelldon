---
baseline_commit: 5793c0c7f67331d372b6eb96ae751a1a946b6012
---

# Story 1.8: End-to-end turn — message in, reply out, face reacts

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want a message I send to produce an LLM reply and a visible face change,
so that the walking skeleton is genuinely alive end-to-end (AD-9, AD-3, AD-12, AD-13, AD-5).

## Acceptance Criteria

1. **Full turn round-trip:** Given all of core, broker (1.4), fork-server (1.5), transport (1.6), and display (1.7) running, when I send a message over the chat adapter, then core's arbiter spawns a **single** worker turn, the broker returns an LLM reply, the reply is delivered back over the chat adapter, and the display reflects the pet's state — all within tolerable latency.
2. **Coalesce, never drop, never double-spawn:** Given a turn is already running, when I send another message, then it is **not dropped silently** — it coalesces into the next turn (no second concurrent worker).
3. **Graceful degrade, never hang:** Given the broker's single retry is exhausted on a transient error, when the turn cannot complete, then the pet surfaces a graceful "can't think right now" state rather than hanging (full fallback/degradation is Epic 2).

## Tasks / Subtasks

> **This is the integration story — it builds the core *runtime* that ties the five actors together around a real turn.** Everything below already exists as isolated, tested pieces (1.1–1.7); 1.8 wires them. The new code is small: a way for core to *emit* envelopes, a grown arbiter (≤1 + coalescing), and a core orchestration loop. Resist rebuilding anything.
>
> **Test seam (read first):** the end-to-end tests run all five actors as **in-process asyncio tasks** against one `BusServer` (exactly how 1.6/1.7 tested), with the worker spawned via the fork-server's **injected `spawn` seam** running `run_worker` in-process — so the full turn is exercised **cross-platform, no real `os.fork()`**. The real-fork process path stays Linux-gated (1.5). Provider is always a fake (no network).
>
> **Dependency inversion (keep `core/` clean):** core must NOT import `worker/`. The orchestrator depends on an injected **spawner** (anything with `async spawn_turn(turn_id, prompt)` / `async reap_current()` / `async ready()`); the composition root (the test, or a later `app.py`) injects the real `ForkServer`. This keeps the `core` LLM-free import-linter green and avoids a core→adapter dependency.

- [x] **Task 1: Core can emit envelopes onto the bus (`core/bus`)** (AC: 1, 3)
  - [x] Add a public `async def deliver(self, env: Envelope) -> None` to `BusServer` that routes an envelope exactly like an inbound one (reuse the existing `_route`). This is the first time **core originates** traffic (OUTBOUND_MSG, STATE_SNAPSHOT); until now core only received. Keep it a thin wrapper over `_route` — no new routing logic.
  - [x] Quick test in `tests/test_bus_routing.py` (or a small new test): `core.deliver(OUTBOUND_MSG→CHAT_TRANSPORT)` reaches a registered CHAT_TRANSPORT client; `core.deliver(STATE_SNAPSHOT→DISPLAY)` reaches a registered DISPLAY client.

- [x] **Task 2: Grow the Arbiter — ≤1 + single-slot coalescing (AD-9)** (AC: 1, 2)
  - [x] Extend `shelldon/core/arbiter.py` (the 1.5 ≤1 skeleton) with a **single pending catch-up slot**. Replace/augment `try_begin`/`end` with the turn lifecycle:
    - `submit(text: str) -> str | None`: if no turn in flight, reserve the slot and **return the prompt to start now**; if a turn IS in flight, **append `text` to the pending slot and return `None`** (coalesced — never dropped, AC2).
    - `complete() -> str | None`: release the slot; if pending messages accumulated, **reserve again, fold them into one prompt (e.g. join with newlines), clear pending, and return it** (drive exactly one catch-up turn); else return `None`. (AD-9: "events coalesce into a single pending catch-up slot — never a growing backlog of turns.")
  - [x] Keep it **pure policy** (no I/O, no asyncio) — the runtime calls it. The folded-prompt shaping is minimal; richer merge/dedup is later.
  - [x] Unit tests in `tests/test_arbiter.py`: submit-when-free returns the prompt + marks in-flight; submit-when-busy returns `None` and accumulates; `complete()` with pending re-reserves and returns the folded prompt; `complete()` with nothing releases and returns `None`; two submits during one turn fold into **one** next prompt (no backlog).

- [x] **Task 3: The core runtime / turn orchestrator (`core/runtime.py`)** (AC: 1, 2, 3)
  - [x] New `shelldon/core/runtime.py`: a `Core` that owns the `BusServer`, a `TurnFence` (1.5), an `Arbiter` (Task 2), an injected **spawner**, a monotonic display `seq`, and a configurable turn timeout. It does NOT import `worker/` or any provider lib (LLM-free, AD-1).
  - [x] `async def run(self)`: `await bus.start()`, ensure the spawner is ready (`await spawner.ready()`), then loop `env = await bus.core_inbox.get()` and dispatch by `env.kind`:
    - **INBOUND_MSG** → `prompt = arbiter.submit(body.text)`; if `prompt is not None`, **start a turn**.
    - **RESULT** → if `not fence.accept(env)`: **discard** (late/zombie/superseded — AD-12); else: disarm the timeout, `fence.close(turn_id)`, then on `result.ok` emit the reply + a reply face, on `not result.ok` **degrade** (AC3); finally `folded = arbiter.complete()` and if `folded is not None`, **start the next (catch-up) turn**.
  - [x] `start_turn(prompt)`: mint a `turn_id` (`uuid4().hex`), `fence.open(turn_id)`, push a "thinking" face snapshot (face reacts), `await spawner.spawn_turn(turn_id, prompt)`, schedule a background `reap_current()` (reclaim the worker — fire-and-forget worker; the Result returns over the bus independently), and **arm a turn timeout** for `turn_id`.
  - [x] **Emit helpers** (use `bus.deliver`): `_send_reply(text)` → `Envelope(OUTBOUND_MSG, src=CORE, dst=CHAT_TRANSPORT, OutboundMessage(text))`; `_push_face(face)` → `Envelope(STATE_SNAPSHOT, src=CORE, dst=DISPLAY, StateSnapshot(Region.FACE, seq=next_seq, face))` with a strictly increasing `seq`.
  - [x] **Degrade (AC3):** `_degrade()` → `_send_reply("…can't think right now…")` + `_push_face(<error/low face token>)`. Called on a failure Result AND on turn timeout.
  - [x] **Turn timeout (AC3 "rather than hanging"):** arm a bounded timer when a turn starts; if it fires before a Result is accepted, `fence.close(turn_id)` (so a late Result is then discarded by the fence — AD-12), `_degrade()`, and `arbiter.complete()` → maybe start the coalesced next turn. Keep the timer minimal (a tracked `asyncio.TimerHandle`/task per current turn, cancelled when the Result lands). Full watchdog/supersession escalation is Epic 2 — note it.
  - [x] **Faces are placeholder tokens** (e.g. `"thinking"`, `"happy"`, `"cant-think"`): the real expression vocabulary + mood→face mapping is **Story 3.3**, and the persistent personality-state struct is **Epic 3 / Story 3.1**. 1.8 pushes lifecycle face tokens only — do NOT build a personality struct here.
  - [x] **Prompt assembly is trivial in 1.8:** the prompt IS the owner's message text (or the folded coalesced text). Real history + curated-memory injection is **Epic 4** — do NOT build it.

- [x] **Task 4: Compose the fork-server as the spawner (in-process seam + production note)** (AC: 1)
  - [x] In the integration tests (and as the documented production shape), construct a real `ForkServer(socket_path, spawn=<in-process seam>, reap=<await the task>)`: the in-process `spawn(socket_path, turn_id, prompt)` does `asyncio.create_task(run_worker(...))` and returns the task; `reap` `await`s it. `preload()` with no modules (the freeze/fork mechanics are 1.5's concern; 1.8 just needs the lifecycle). This runs the worker **in-process, cross-platform** — the real `os.fork()` path is already Linux-gated in 1.5.
  - [x] This **wires the two ≤1 guards** the 1.5 review left independent: the **Arbiter** is the single *policy* gate (won't request a second spawn until `complete()`), and the **ForkServer.worker_in_flight** is the *mechanical* backstop — they never conflict because the arbiter serializes. Document that this resolves the 1.5 deferred "Arbiter and ForkServer.worker_in_flight are independent" item. Also note the single-task core loop makes `Arbiter` access serial (no lock needed — resolves the 1.5 "try_begin not async-safe" item for the single-consumer design).
  - [x] **Production process/IPC note (do NOT build):** in production the fork-server runs as its **own single-threaded process** driven by core over an IPC control channel (never fork from the asyncio loop — 1.5). 1.8 proves the turn *lifecycle* end-to-end in-process; the literal multi-process deployment + IPC control channel is a later deployment-hardening story. Record it, don't build it.

- [x] **Task 5: AC1 end-to-end integration test** (AC: 1)
  - [x] New `tests/test_end_to_end_turn.py`: start a `Core` + connect `run_broker` (fake OK provider), `run_display` (StubRenderer), `run_cli_transport` (injected inbound/outbound), all as in-process tasks on one socket; spawner = `ForkServer` with the in-process seam.
  - [x] Feed one owner line → assert: (a) the CLI **outbound sink** receives the broker's reply text; (b) the display **stub** rendered at least one `StateSnapshot` (face reacted); (c) **exactly one** worker was spawned (track a concurrency counter in the in-process spawn — max concurrent == 1). Bound everything with timeouts so a wiring miss fails fast, not hangs.

- [x] **Task 6: AC2 coalescing integration test** (AC: 2)
  - [x] New test (same file): make the turn **slow** (a gated fake provider or a gated worker seam) so a turn is in flight when a second message arrives. Feed message A, then message B while A's turn is in flight. Assert: **never two concurrent workers** (the spawn concurrency counter never exceeds 1); B is **not dropped** — after A completes, a **second** turn runs folding B in, and B's reply is delivered. (Assert the arbiter coalesced: one in-flight + one pending → exactly two turns total, never two at once.)

- [x] **Task 7: AC3 degrade + no-hang tests** (AC: 3)
  - [x] **Failure Result:** fake provider raises a transient error on every attempt → the broker (1.4) returns `Result(ok=False, …)` after its single retry → assert the CLI receives the graceful **"can't think right now"** text AND a corresponding error/low **face snapshot** is pushed; the run does not hang (bounded by a timeout).
  - [x] **Turn timeout (no-hang):** with a spawner/worker that never produces a Result (e.g. a worker seam that connects but sends no Job), assert that within the configured turn timeout the pet **degrades** (same graceful state) rather than hanging, the turn is closed, and a **late Result** arriving afterward is **discarded** by the fence (AD-12) — no double-delivery, no second turn from the stale Result.

- [x] **Task 8: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → all contracts KEPT. **`core/` stays LLM-free:** `core/runtime.py` imports only contracts + core (bus/arbiter/turn) + stdlib, NOT `worker/` and NOT any provider lib (the spawner is injected). Confirm the import-linter passes with the new `core/runtime.py`.
  - [x] `uv run pytest -q` → all green (prior suites + the new integration tests). No new runtime dependency (uuid/asyncio stdlib; msgspec pinned).

## Dev Notes

### Architecture compliance (binding)

- **AD-9 — The arbiter governs the brain:** ≤1 worker turn in flight; events during a turn **coalesce into a single pending catch-up slot** (the next turn folds in everything since it started — never a growing backlog); on provider-chain exhaustion the arbiter **falls back to a reflex/graceful behavior** so the pet never freezes. 1.8 builds the ≤1 + coalescing + degrade-to-graceful; cooldown, credit/battery budget, and the full provider chain are Epic 2 / Epic 5. [Source: ARCHITECTURE-SPINE.md#AD-9]
- **AD-12 — Turn identity & idempotent close:** every turn carries a `turn_id`; core **fences** on it; a `Result` whose `turn_id` is closed/superseded/unknown is **discarded**; close is idempotent. The timeout path closes the turn so a late Result is discarded. (`TurnFence` already implements this — 1.5.) [Source: ARCHITECTURE-SPINE.md#AD-12]
- **AD-3 — Fork-server ephemeral workers:** one worker per turn, ≤1 in flight, fire-and-forget (the worker sends its Job and dies; the broker's Result routes to core). 1.8 drives the `ForkServer` via its injected spawn seam; the real fork + the fork-server-as-its-own-process are 1.5/deployment. [Source: ARCHITECTURE-SPINE.md#AD-3]
- **AD-13 — Chat transport round-trip:** owner message arrives as INBOUND_MSG; the reply leaves as OUTBOUND_MSG over the same adapter; a transport/turn failure **degrades gracefully, never crashes core**. [Source: ARCHITECTURE-SPINE.md#AD-13]
- **AD-5 — Display reflects core-pushed state:** core pushes a `StateSnapshot` (monotonic `seq`, FACE region) on turn lifecycle events; the display renders latest-wins (1.7). 1.8's faces are placeholder tokens (real expressions = 3.3; personality struct = Epic 3). [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-1 — LLM-free core:** the new `core/runtime.py` orchestrates but imports no provider lib and not `worker/` (dependency-inverted spawner). Import-linter stays green. [Source: ARCHITECTURE-SPINE.md#AD-1]

### The turn lifecycle (the heart of 1.8)

```
INBOUND_MSG(text) ──▶ core_inbox ──▶ arbiter.submit(text)
        │                                   │
        │                        free? ─yes─▶ start_turn(text)
        │                                   │     fence.open(turn_id); push "thinking" face
        │                        busy? ─────▶ coalesce into pending slot (return None)
        │                                         spawner.spawn_turn(turn_id, text)
        ▼                                         schedule reap_current(); arm timeout
   worker (fire-and-forget): connect, send Job(payload=text)→BROKER, exit
        ▼
   broker: handle_job → Result(ok/err), echo turn_id → RESULT→CORE
        ▼
   core_inbox ──▶ fence.accept(env)?
        ├─ no  ──▶ discard (late/zombie/superseded; AD-12)
        └─ yes ──▶ disarm timeout; fence.close(turn_id)
                      ok    ──▶ OUTBOUND_MSG(reply)→transport + StateSnapshot(reply face)→display
                      error ──▶ degrade: OUTBOUND_MSG("can't think…") + error face
                   folded = arbiter.complete()
                      pending? ──▶ start_turn(folded)   # exactly one catch-up turn
   timeout fires before a Result ──▶ fence.close(turn_id); degrade; arbiter.complete()→maybe next turn
```

Everything in this diagram except `arbiter.submit/complete`, `start_turn`, the emit helpers, the timeout, and `bus.deliver` **already exists**. Build only those.

### Wiring the two ≤1 guards (resolves 1.5 deferred items)

The 1.5 review deferred two items to 1.8:
- *"Arbiter and ForkServer.worker_in_flight are independent and never connected."* → Now connected by the runtime: the **Arbiter is the single policy gate** (it won't ask the spawner for a second turn until `complete()`), and **ForkServer.worker_in_flight is the mechanical backstop**. Because the arbiter serializes turn requests, the fork-server never sees a busy conflict in normal flow. Document this in completion notes.
- *"Arbiter.try_begin not async-safe (no lock between read-check and write)."* → The core runtime is a **single-consumer loop** (`while: await core_inbox.get()`), so arbiter access is serial — no `await` interleaves a second submit mid-decision. No lock needed for this design; note it. (A lock returns only if core ever multi-tasks turn admission — not now.)

### Previous story intelligence (1.1–1.7) — what to reuse verbatim

- **Bus + emit:** `BusServer` (core/bus) hosts `core_inbox` and `_route`. Core reads `core_inbox`; add `deliver()` to emit. Clients (`run_broker`/`run_display`/`run_cli_transport`) connect via `connect(...)`. [Source: 1.3]
- **Two-task supervise/teardown + injected seams:** the integration harness mirrors 1.6/1.7 — start actors as tasks, poll `srv._registry.get(actor)` to await registration (or `asyncio.sleep(0.05)`), and on teardown cancel tasks then `await core.bus.stop()` (the 1.7 fix means stop() won't hang on idle clients). **Keep BOTH stream ends of every connection alive** (the 1.6/1.7 gotcha). [Source: 1.6/1.7]
- **ForkServer seam:** `ForkServer(socket_path, spawn=..., reap=...)`; `spawn(socket_path, turn_id, prompt)` → in-process `asyncio.create_task(run_worker(...))`; `reap` awaits it. `run_worker(socket_path, turn_id, prompt)` connects as WORKER and sends `Job(payload=prompt)` with `turn_id`. [Source: 1.5 forkserver.py / worker.py]
- **Broker:** `run_broker(socket_path, provider)`; `handle_job` returns `Result(ok=True, payload=text)` on success and `Result(ok=False, error=…)` after the single retry is exhausted (transient) or on any error. The fake provider is a class with `async def complete(self, prompt) -> str` (raise `TransientProviderError` to force the AC3 failure path). [Source: 1.4 broker/broker.py, provider.py]
- **Transport:** `run_cli_transport(socket_path, *, inbound, outbound)` — inject a controllable inbound source (the 1.6 `_Source` queue pattern) and an outbound list sink; INBOUND_MSG out, OUTBOUND_MSG rendered. [Source: 1.6 transport/cli.py]
- **Display:** `run_display(socket_path, renderer)` with `StubRenderer` (records `rendered: list[StateSnapshot]`); latest-wins per region by strict-greater `seq` — so core's pushed `seq` must strictly increase. [Source: 1.7 display/service.py, renderer.py]
- **Contracts ready:** `InboundMessage`/`OutboundMessage`/`StateSnapshot`/`Region.FACE` + routing (INBOUND→CORE, OUTBOUND→CHAT_TRANSPORT, STATE_SNAPSHOT→DISPLAY, JOB→BROKER, RESULT→CORE) all exist (1.2/1.6/1.7) — **no contract change expected** in 1.8. [Source: contracts/__init__.py]

### Project Structure Notes

- New: `shelldon/core/runtime.py` (the orchestrator). Modified: `shelldon/core/arbiter.py` (coalescing), `shelldon/core/bus/server.py` (public `deliver`). Tests: `tests/test_end_to_end_turn.py` (new), `tests/test_arbiter.py` (extend), `tests/test_bus_routing.py` (extend for `deliver`). **`core/` is touched this story (unlike 1.6/1.7) — that's expected: 1.8 builds the core runtime.** The import-linter "core is LLM-free" must stay KEPT (runtime imports no provider lib, no `worker/`). [Source: ARCHITECTURE-SPINE.md#Structural Seed, #AD-1]

### Scope boundary (prevent scope creep)

**IN scope (1.8):** `bus.deliver` (core emits); arbiter ≤1 + single-slot coalescing + degrade-on-failure; the `core/runtime.py` turn orchestrator (inbound→turn→reply+face, fence, timeout, degrade); in-process fork-server composition; the AC1/AC2/AC3 end-to-end integration tests + arbiter unit tests.

**OUT of scope (later, do NOT build):**
- **Real prompt assembly** (history window + curated memory) → **Epic 4**. 1.8's prompt is the message text.
- **Persistent personality-state struct + real expressions** → **Epic 3 (3.1 struct, 3.2 reflexes, 3.3 faces)**. 1.8 pushes placeholder face tokens on turn lifecycle only.
- **Full arbiter** — cooldown, daily credit/turn budget, battery backoff, proactive turns → **Epic 5**; **provider chain / fallback / true degrade-to-reflex** → **Epic 2**. 1.8 does single-retry-then-graceful + a minimal turn timeout.
- **Literal multi-process deployment + fork-server IPC control channel** → deployment-hardening. 1.8 wires in-process and documents the process shape.
- **Real `os.fork()` end-to-end** → Linux-gated (already in 1.5). 1.8's e2e uses the in-process spawn seam.
- **Transport/display supervision & auto-restart** → Epic 2.

### Testing standards

- `pytest` + `pytest-asyncio` (auto), mirroring package layout. The end-to-end tests run all five actors in-process on one `BusServer`, with the worker spawned via the fork-server's in-process seam (cross-platform, no real fork). Use controllable inbound (1.6 `_Source`), an outbound list sink, a `StubRenderer`, and a fake provider (OK / gated-slow / always-transient-error) to drive AC1/AC2/AC3 deterministically. Track a **spawn-concurrency counter** in the in-process spawn to assert ≤1 (AC1/AC2). Bound every wait with a timeout so a wiring miss fails fast. Arbiter coalescing is also unit-tested in isolation (pure, no I/O).
- Before marking done: `uv run lint-imports` (core LLM-free KEPT) and `uv run pytest -q` (green).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.8; #Epic 1 cross-cutting (1.8 confirms the wiring, isolation tests precede it)]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-9, #AD-12, #AD-3, #AD-13, #AD-5, #AD-1, #AD-11]
- [Source: shelldon/core/arbiter.py (≤1 skeleton to grow), core/turn.py (TurnFence, AD-12), core/bus/server.py (_route to wrap as deliver)]
- [Source: shelldon/worker/forkserver.py (spawn/reap/ready seam), worker/worker.py (run_worker fire-and-forget); broker/broker.py + service.py (Result ok/err shape, run_broker); transport/cli.py (run_cli_transport seams); display/service.py + renderer.py (run_display, StubRenderer, strict-greater seq)]
- [Source: _bmad-output/implementation-artifacts/1-5-…md (the two ≤1 guards + async-safety items 1.8 resolves); 1-6/1-7 (in-process integration harness pattern, keep-both-stream-ends, stop() no longer hangs)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (1M context)

### Debug Log References

- `uv run pytest -q` → 86 passed, 1 skipped (the Linux-gated real-fork test), 0 failed.
- `uv run lint-imports` → 2 contracts kept, 0 broken ("core is LLM-free (AD-1)" KEPT with the new `core/runtime.py`).

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- **bus.deliver (Task 1):** thin `async deliver()` over the existing `_route` — core now ORIGINATES OUTBOUND_MSG/STATE_SNAPSHOT; no new routing logic.
- **Arbiter coalescing (Task 2):** replaced the 1.5 `try_begin`/`end` skeleton with `submit`/`complete` + a single pending list. `submit` starts a turn when free or folds-into-pending when busy (never drops, AC2); `complete` drives exactly one newline-folded catch-up turn or releases. Pure policy, no I/O.
- **core/runtime.py (Task 3):** single-consumer `Core` loop over `bus.core_inbox` owning bus + `TurnFence` + `Arbiter` + injected spawner + monotonic display `seq` + turn timeout. Faces are placeholder tokens (`thinking`/`happy`/`cant-think`). Degrade = graceful reply + error face, fired on a failure Result AND on turn timeout. The timeout is disarmed synchronously before any await in the Result path so it can't race; on fire it closes the turn (so a late Result is fenced out, AD-12), degrades, and may start the coalesced next turn.
- **Two ≤1 guards wired (Task 4, resolves 1.5 deferred items):** the **Arbiter** is the single *policy* gate (won't request a second spawn until `complete()`) and **ForkServer.worker_in_flight** is the *mechanical* backstop — they never conflict because the arbiter serializes turn requests. The **single-consumer core loop** makes Arbiter access serial (no `await` interleaves a second submit mid-decision) — so no lock is needed, resolving the 1.5 "try_begin not async-safe" item for this design.
- **Production process/IPC shape (recorded, NOT built):** the fork-server runs as its own single-threaded process driven by core over an IPC control channel (never fork from the asyncio loop). 1.8 proves the turn lifecycle in-process via the injected spawn seam (`asyncio.create_task(run_worker(...))`, cross-platform); multi-process + IPC is later deployment-hardening. Real `os.fork()` stays Linux-gated (1.5).
- **AD-1 held:** `core/runtime.py` imports only contracts + core (bus/arbiter/turn) + stdlib (asyncio/logging/uuid); the spawner is duck-typed/injected, so no `worker/` and no provider import. Import-linter "core is LLM-free" KEPT.
- No new runtime dependency added (uuid/asyncio are stdlib; msgspec stays pinned).

### File List

- `shelldon/core/bus/server.py` (modified — public `async deliver()` wrapping `_route`)
- `shelldon/core/arbiter.py` (modified — `submit`/`complete` ≤1 + single-slot coalescing; `reset()` for failed-to-start release [review P1])
- `shelldon/core/runtime.py` (new — the `Core` turn orchestrator; `_start_turn` spawn-failure guard release [review P1])
- `tests/test_bus_routing.py` (modified — `deliver` → transport/display routing tests)
- `tests/test_arbiter.py` (modified — new submit/complete coalescing unit tests)
- `tests/test_end_to_end_turn.py` (new — AC1/AC2/AC3 in-process end-to-end tests)
- `tests/test_runtime.py` (new — `_start_turn` spawn-failure guard-release test [review P1])

## Review Findings (2026-06-16)

Reviewers: Blind Hunter · Edge Case Hunter · Acceptance Auditor

### Patches (left as action items)

- [x] `[Review][Patch]` **`_start_turn` no exception handler on `spawn_turn()`** — `shelldon/core/runtime.py:_start_turn` — two real crash/stuck paths: (1) any spawn failure (OS error, etc.) leaves `fence.open()` unclosed and `arbiter.worker_in_flight=True` permanently; all future turns silently coalesce into a pending slot that never flushes. (2) timeout + pending coalesce race: `_timeout_watch` fires with pending messages → `arbiter.complete()` re-reserves → `_start_turn(folded)` → `ForkServer.worker_in_flight` is still `True` (old reap task still running) → `spawn_turn()` raises `WorkerBusyError` → propagates silently through the background task, leaving fence/arbiter stuck. Fix: wrap `spawner.spawn_turn()` in try/except inside `_start_turn`, close fence and reset `arbiter.worker_in_flight = False` on failure. Add a test for the spawn-failure release path.
  - **RESOLVED (2026-06-16):** wrapped `spawner.spawn_turn()` in `_start_turn` with `except Exception`; on failure it logs, `fence.close(turn_id)`, and calls a new `Arbiter.reset()` (releases the slot AND clears any pending). Covers both the OS-spawn-error path and the `WorkerBusyError` timeout+catch-up race. New `tests/test_runtime.py` asserts both guards release and a fresh turn can start afterward (parametrized over `RuntimeError` and `WorkerBusyError`). The dropped catch-up prompt is the accepted deferred behavior (Epic 2). Full suite 88 passed / 1 skipped; import contracts KEPT.

### Deferred

- `[Review][Defer]` Timeout + pending catch-up: when P1 catches `WorkerBusyError`, the coalesced catch-up prompt is dropped (not re-queued). Full guaranteed delivery for timed-out catch-ups (watchdog/reschedule) is Epic 2 scope.
- `[Review][Defer]` `_handle_result` — `arbiter.complete()` not called if `bus.deliver` raises mid-reply. `_route` already catches `OSError`, so low risk; handle in resilience story.
- `[Review][Defer]` AC1 test asserts `len(renderer.rendered) >= 1` but never checks face token values (`FACE_THINKING`, `FACE_REPLY`). Test quality gap; AC is met.
- `[Review][Defer]` Timeout test timing flakiness — `asyncio.sleep(0.8)` has no anchor to turn start; fix with direct `fence.current` state assertion when hardening.
- `[Review][Defer]` `_await` helper raises bare `AssertionError` with no registry state snapshot — poor CI diagnostics; fix when debugging becomes an issue.

## Change Log

- 2026-06-16 — Story 1.8 implemented: `bus.deliver` (core emits), Arbiter ≤1 + single-slot coalescing, `core/runtime.py` turn orchestrator (inbound→turn→reply+face, fence, timeout, degrade), in-process fork-server composition, and the AC1/AC2/AC3 end-to-end + arbiter unit tests. Full suite green (86 passed, 1 skipped); import-linter contracts kept. Status → review.
- 2026-06-16 — Addressed code review findings — 1 item resolved (review P1: `_start_turn` spawn-failure guard release via `Arbiter.reset()` + `tests/test_runtime.py`). Remaining review items are deferred to Epic 2 / hardening. Full suite 88 passed / 1 skipped; import contracts KEPT. Status → review.
