---
baseline_commit: 3ca7e7c75c616d1e6aa31384a3aa871a918721f7
---
# Story 3.2: Resident reflex loop

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to drift its mood and energy on its own between messages — on a basic in-core tick, even with the network down,
so that it feels like a living creature with an inner life, not a frozen screen (AD-1, AD-5, AD-9, CAP-2).

## Acceptance Criteria

1. **Reflexes run on an in-core tick and mutate state, no LLM, even offline:** Given no LLM turn is active (including network offline), when time passes, then in-core reflexes (time-of-day mood drift + idle-based energy/mood drift) run on a **basic in-core tick** and mutate the personality-state struct **in-process via the Story 3.1 `apply_patch` API**, with **no LLM call and no provider/worker involvement**. The tick runs regardless of network state (it never touches the broker/chain).
2. **Reflexes and a turn coexist without fighting over state:** Given reflexes are running, when an LLM turn begins (and while it is in flight), then reflexes and the turn **coexist without fighting over state** — the single-writer core serializes every mutation (the reflex tick and the turn handler are both tasks on the one core event loop, and `apply_patch` is synchronous, so no mutation can interleave mid-write). Reflexes are **not paused** by a turn; both write through the same single-writer path.
3. **Tick is subsumable by the Epic 5 scheduler with no behavior change:** Given the reflex tick, when the scheduler arrives in Epic 5 (Story 5.1), then this tick is **structured so it can later be subsumed as a cost-tier "reflex job" without changing reflex behavior** — the *what to drift* (a pure reflex function producing a sparse patch) is separable from the *when to run it* (the interval driver), so 5.1 can call the same function on its own cadence. It works **standalone now** with **no forward dependency** on the scheduler.

> **Scope seam (binding):** 3.2 builds the **reflex tick + the state drift it writes** — a periodic in-core driver that calls a pure reflex function, which returns a sparse patch over the **existing** Story 3.1 closed dotted paths (`mood.valence`, `mood.arousal`, `energy`), applied via `apply_patch`. It does **NOT** build: the **mood→face expression mapping or any face rendering** (blink, idle animation, the starter emotion set) — that is **Story 3.3**, which reads this drifting state and renders it; the **scheduler** that will own the tick's cadence — **Epic 5 (Story 5.1)**; any **new state fields / contract changes** (it drifts the paths 3.1 already defined); **arbiter changes** (reflexes are cheap in-core writes, not arbiter-gated turns). The single biggest mistake here is building 3.3's faces/blink-rendering inside 3.2, or extending the state struct / `contracts/`.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent):**
> - **Story 3.1 shipped the entire state substrate** in `shelldon/core/state.py`: a mutable `PersonalityState` (`mood.valence`/`mood.arousal`, `energy`, `last_interaction`), the closed `WRITABLE_PATHS` set, and `PersistentState.apply_patch(patch)` (validates against the closed set, marks `_dirty`). **3.2 is a CONSUMER of this API — it adds no new state, no new paths, no new file in `state.py`.** [Source: shelldon/core/state.py]
> - `core/runtime.py::Core` already runs a **single in-core periodic background task** for the 3.1 checkpoint flush: `self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())`, started in `run()`, cancelled in `_cleanup()`. It catches `asyncio.CancelledError` to exit cleanly and wraps the per-iteration body so one error doesn't kill the loop. **The reflex tick is a SECOND task in this exact shape** — a separate `self._reflex_task` slot (not `_bg`, which is for transient per-turn reap tasks that must drain to 0 — see the 1.9 soak). [Source: shelldon/core/runtime.py:_checkpoint_loop, _cleanup]
> - **The reflex tick writes → marks dirty → the existing 3.1 checkpoint loop persists it.** This is the intended composition: 3.2 produces the high-churn dirty writes that 3.1's dirty-flag + periodic flush were built to keep off the SD card (NFR7). 3.2 adds **no new disk write**. [Source: shelldon/core/runtime.py, ARCHITECTURE-SPINE.md#AD-7]
> - `Core` is the **single-consumer serial loop** over `bus.core_inbox`; the arbiter/fence are accessed without a lock because access is serial. Reflex writes inherit the same property: `apply_patch` is synchronous (no `await`), so a reflex tick and a turn handler can't interleave mid-mutation on the one event loop — this IS the AC2 single-writer serialization. [Source: shelldon/core/runtime.py:69-90, shelldon/core/arbiter.py:1-12]
> - **No clock injection exists yet.** Time-of-day drift needs "now," but tests must be deterministic (Epic 2 retro #1: prefer state-predicate asserts over sleep/wall-clock). Make the reflex function take `now` (and the prior state) as **parameters** and return a patch — a pure function — so tests call it directly with a fixed `now`, and the tick driver passes real UTC now. [Source: epic-2-retro-2026-06-17.md#action-items]

- [x] **Task 1: A pure reflex function — (state, now) → sparse patch** (AC: 1, 3)
  - [x] In `shelldon/core/state.py` (or a small `core/reflexes.py` — prefer `reflexes.py` to keep the substrate and the policy separate), add a **pure function** e.g. `compute_reflex_patch(state: PersonalityState, now: datetime, last_tick: datetime | None) -> dict` that returns a sparse patch over the **existing** closed paths only (`mood.valence`/`mood.arousal`/`energy`). No I/O, no mutation, no `apply_patch` call inside — it just computes the delta. This is the unit the Epic 5 scheduler will later call as a "reflex job" (AC3 separability).
  - [x] **Time-of-day mood drift:** derive a small valence/arousal nudge from `now` (e.g. calmer/lower-arousal at night, livelier midday). Keep the model **minimal and bounded** — clamp outputs to a sane range (e.g. valence/arousal in [-1, 1], energy in [0, 1]); do NOT build a rich affect engine. Use UTC consistently (matches the ISO-8601-UTC `last_interaction` convention).
  - [x] **Idle-based drift:** using `state.last_interaction` (and/or `last_tick`), drift energy/mood toward a resting baseline as idle time grows (the pet "settles" when not interacted with). Bounded and clamped as above.
  - [x] Return an **empty patch** (`{}`) when nothing should change this tick, so the tick can skip a no-op `apply_patch` (don't mark dirty for nothing — keeps the 3.1 flush idle when truly idle).

- [x] **Task 2: The in-core reflex tick driver in Core** (AC: 1, 2)
  - [x] Add a periodic reflex tick to `Core` mirroring the 3.1 checkpoint-loop shape: a `self._reflex_task` started in `run()`, an `async def _reflex_loop(self)` that, on an interval (`reflex_interval`, injectable; small in tests), computes the patch via the pure function with real UTC `now` and applies it through `self.state.apply_patch(...)` **only if the patch is non-empty**. Validate `reflex_interval > 0` in `__init__` (same guard pattern as `checkpoint_interval` from the 3.1 review). Wrap the per-iteration body so one error logs + continues (don't let a bad tick kill the loop — the 3.1 review precedent), and catch `CancelledError` to exit cleanly.
  - [x] Cancel `self._reflex_task` in `_cleanup()` alongside `_checkpoint_task` (teardown must not hang). Do **not** put it in `self._bg`.
  - [x] **No LLM / no network (AC1):** the tick calls only `self.state.apply_patch` — it must never touch the broker, the spawner, or emit a `Job`. Keep it purely in-core. (Pushing the drifting mood to the display as a face snapshot is **Story 3.3** — do not wire `_push_face` from the reflex tick here.)

- [x] **Task 3: Feed the idle signal — update `last_interaction` on a turn** (AC: 1, 2)
  - [x] So idle drift has a real signal, set `last_interaction` (ISO-8601 UTC) via `self.state.apply_patch({"last_interaction": ...})` from the core loop when a turn occurs (e.g. when an `INBOUND_MSG` is admitted, or on reply). Keep it minimal and in-core (single writer). This is the only new state write on the turn path; it must not change turn behavior or ordering.
  - [x] This write goes through the same single-writer `apply_patch`, demonstrating AC2 coexistence: the turn path and the reflex tick both mutate state through one serialized API.

- [x] **Task 4: Tests** (AC: 1, 2, 3)
  - [x] **AC1 (pure function, deterministic):** call `compute_reflex_patch` directly with a fixed `now` (e.g. a night timestamp vs. a midday timestamp) and assert the returned patch nudges `mood`/`energy` in the expected direction and **stays clamped**; assert an idle vs. recently-interacted `state` produces the expected idle drift; assert a no-change condition returns `{}`. No sleeps, no wall-clock.
  - [x] **AC1 (tick applies, no LLM/network):** drive the tick deterministically (tiny `reflex_interval`, or call the loop body / pure-function path directly) against an injected `tmp_path` checkpoint and a spawner/broker that would **raise if touched**, asserting the reflex still mutates RAM state with the network "down" (prove no broker/spawner call). Prefer state-predicate polling over fixed sleeps.
  - [x] **AC2 (coexist, single-writer):** assert a reflex write and a turn-path `last_interaction` write both land via `apply_patch` without corruption, and that a reflex applied "during" a turn leaves a consistent struct (since `apply_patch` is synchronous, assert the resulting RAM values are the composition of both writes — no half-applied state).
  - [x] **AC3 (subsumable):** assert the pure `compute_reflex_patch` is callable in isolation (no `Core`, no tick) and returns the same patch a scheduler "reflex job" would apply — proving the *what* is separable from the *when*. Optionally assert the tick driver is the only thing that knows the interval.
  - [x] Use injected `tmp_path` checkpoint files; **never write real `$HOME`**. Reuse the `_DummySpawner` / predicate-poll patterns from `tests/test_state.py`.

- [x] **Task 5: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (3.2 is pure `core/` + `tests/`; no provider/LLM import enters `core/`; AD-1 holds).
  - [x] `uv run pytest -q` → green (the existing 159 unchanged + the new reflex tests). Default run hits no network and writes no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **CAP-2 — Aliveness / resident reflexes:** "Between LLM turns the pet visibly lives … driven by resident reflexes reading a persistent state struct, independent of the brain and working offline." 3.2 is the reflex driver half of CAP-2; the visible/face half is 3.3. [Source: ARCHITECTURE-SPINE.md#CAP-2 (AD-1, AD-5, AD-9)]
- **AD-1 — LLM-free core:** reflexes run **in-core with no LLM** — "reflex jobs run in-core, no LLM, cheap CPU." The tick adds only `core/` code; import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1, #AD-14]
- **AD-5 — Core is the sole writer; reflexes mutate state in-process:** "Reflexes mutate state in-process. … The state delta is a **sparse patch over fixed dotted paths**." 3.2's reflex writes go through the Story 3.1 `apply_patch` over the existing closed paths — no new write path, no worker/bus write. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-9 — The arbiter governs the brain (reflex-vs-turn):** "on provider-chain exhaustion the arbiter falls back to a reflex behavior so the pet never freezes." 3.2 does NOT need to extend the arbiter: reflexes are cheap in-core writes that always run; AC2 coexistence is solved by single-writer serialization, not by arbiter gating. Richer reflex-vs-turn arbitration is later. [Source: ARCHITECTURE-SPINE.md#AD-9]
- **AD-7 / NFR7 — RAM state, periodic checkpoint, SD-wear:** reflexes are exactly the "high-frequency reflex/state churn" AD-7 keeps in RAM. 3.2 produces the dirty writes; the 3.1 dirty-flag + periodic flush (already built) is what bounds disk writes. 3.2 adds **no new disk write**. [Source: ARCHITECTURE-SPINE.md#AD-7, epics.md#NFR7]
- **AD-14 — Scheduler subsumes the tick (forward seam, no forward dependency):** Story 5.1's AC says "Given Epic 3's reflex tick … reflexes run as cost-tier 'reflex jobs' on the scheduler with **unchanged behavior**." Keep the *reflex computation* (pure function) separable from the *scheduling* (interval driver) so 5.1 wraps it without changing behavior. Same standalone-now/subsumable-later shape as 2.3's degrade ack and 3.1's checkpoint loop. [Source: epics.md#Story 5.1, ARCHITECTURE-SPINE.md#AD-14]

### Design guidance (what to build, minimally)

- **Pure reflex function + thin driver.** The reflex *policy* (`compute_reflex_patch(state, now, last_tick) -> dict`) is a pure function: deterministic, no I/O, fully unit-testable with a fixed `now`. The *driver* (`Core._reflex_loop`) owns only the interval + the `apply_patch` call. This split IS the AC3 subsumability seam and the Epic-2-retro deterministic-test win. Put the function in a new `core/reflexes.py` (keeps `state.py` as the substrate; `reflexes.py` as the policy) — `core/` only, so import-linter stays KEPT.
- **Reuse the 3.1 background-task shape exactly.** `self._reflex_task` started in `run()`, cancelled in `_cleanup()`, per-iteration `try/except Exception` that logs + continues, outer `except asyncio.CancelledError: return`, and a positive-interval guard in `__init__`. Do not invent a new pattern; do not use `self._bg` (its "drains to 0" invariant is asserted by the 1.9 soak — a permanent resident there breaks it).
- **Bounded, minimal affect.** Drift is small per-tick nudges with hard clamps (valence/arousal ∈ [-1, 1], energy ∈ [0, 1]). This is NOT a psychology model — it's enough motion that the face (3.3) has something to render. Resist building richer affect; that's not in any 3.x AC.
- **No face, no display from 3.2.** The reflex tick must not call `_push_face` or emit a `StateSnapshot`. 3.3 is what reads the drifting state and pushes face snapshots. Wiring state→face here would pre-build 3.3 and is the primary scope risk.
- **Time handling.** Use `datetime` in UTC (stdlib; matches the ISO-8601-UTC `last_interaction` convention). The driver passes `datetime.now(UTC)`; tests pass fixed timestamps to the pure function. No new dependency.

### What 3.2 does NOT do

- **No mood→face mapping, no real expressions, no blink/idle ANIMATION** — that is **Story 3.3** (the starter set content/sleepy/curious/grumpy/excited/low-battery; partial-refresh rendering; the closed `region-id` work). 3.2 only drifts the *state*; 3.3 renders it.
- **No scheduler** — **Epic 5 (5.1)**. The reflex tick is a minimal interim interval driver, structured to be subsumed, not a scheduler.
- **No new state fields, no new closed paths, no `contracts/` change** — 3.2 drifts the paths Story 3.1 already defined (`mood.valence`/`mood.arousal`/`energy`, plus the turn-path `last_interaction` write). If a reflex seems to need a new field, that's a signal it belongs to 3.3 (face) or later — flag it, don't add it here.
- **No arbiter change / no reflex-vs-turn gating** — reflexes are cheap in-core writes that always run; coexistence is via single-writer serialization. The arbiter's reflex-fallback-on-chain-exhaustion already shipped (Story 2.3).
- **No LLM, no worker, no broker, no network** — the tick is purely in-core. If the tick touches the spawner/broker, the design is wrong.
- **No new disk write** — reflex writes mark the struct dirty; the existing 3.1 periodic checkpoint flushes it.

### Project Structure Notes

- **New:** `shelldon/core/reflexes.py` (the pure `compute_reflex_patch` reflex policy). New tests `tests/test_reflexes.py` (pure-function + tick-driver tests), or extend `tests/test_state.py` — prefer a new file for clarity.
- **Modified:** `shelldon/core/runtime.py` — add `reflex_interval` (injectable, positive-guarded), `self._reflex_task` started in `run()` and cancelled in `_cleanup()`, `_reflex_loop`; add the turn-path `last_interaction` write (Task 3). Keep changes surgical — do not refactor the turn/arbiter/fence/checkpoint logic; the reflex tick sits alongside the checkpoint tick.
- `core/` only → import-linter KEPT. Structural Seed lists `core/ … reflexes/` as a domain concern — a single `reflexes.py` is the minimal form (promote to a package later if it grows). [Source: ARCHITECTURE-SPINE.md#Structural-Seed (line 194: `core/ … reflexes/`)]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). The reflex policy is a **pure function** — test it directly with fixed `now`/`state` inputs (deterministic, no sleeps). For the tick driver, drive deterministically (tiny injected `reflex_interval` with state-predicate polling, or exercise the loop body directly) — prefer state-predicate assertions over `sleep` anchors (Epic 2 retro action #1). Use `tmp_path` checkpoint files; **never write real `$HOME`**. Assert reflexes run with a spawner/broker that raises if touched (proves no-LLM/offline). Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green, no network). [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-06-17.md#action-items]

### Previous story intelligence (Story 3.1 — just completed)

- **3.1 delivered the exact API 3.2 consumes:** `PersistentState.apply_patch(patch)` validates each key against `WRITABLE_PATHS = {"mood.valence", "mood.arousal", "energy", "last_interaction"}` (whole-patch reject on an unknown path — fail fast, no half-apply) and sets `_dirty`. `Core.state` is a live `PersistentState`. Reflex writes use this verbatim. [Source: shelldon/core/state.py]
- **3.1's checkpoint loop is the template for 3.2's reflex loop.** It is a singleton task in its own slot (`self._checkpoint_task`, NOT `_bg`), started in `run()`, cancelled in `_cleanup()`, with a per-iteration `try/except Exception` (logs + continues) and an outer `except asyncio.CancelledError: return`. **Copy this shape** for `_reflex_loop`. [Source: shelldon/core/runtime.py:_checkpoint_loop, _cleanup]
- **3.1 review findings to pre-empt (apply the same hardening here):** (1) a non-positive interval must raise `ValueError` in `__init__` — add the same guard for `reflex_interval`; (2) the loop must survive a transient error per iteration (logs + continues), proven by a test; (3) keep the long-lived task OUT of `_bg`. These three were the entire 3.1 code-review patch set — building them in from the start avoids a repeat review cycle. [Source: _bmad-output/implementation-artifacts/3-1-persistent-personality-state-struct.md#Review Findings]
- **`_bg` invariant is load-bearing:** `tests/test_endurance_soak.py` asserts `len(core._bg) == 0` after the run. A permanent reflex task in `_bg` breaks the 1.9 soak — use a dedicated slot. (This is exactly why 3.1's checkpoint task got its own slot.) [Source: tests/test_endurance_soak.py:143]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 3 / Story 3.2 (this story); #Story 3.1 (the state substrate — done); #Story 3.3 (expressions/face — reads this state, next); #Story 5.1 (scheduler subsumes the tick); #NFR7]
- [Source: ARCHITECTURE-SPINE.md#CAP-2 (resident reflexes), #AD-1 (LLM-free core), #AD-5 (core sole writer, sparse dotted-path patches, reflexes mutate in-process), #AD-7 (RAM + periodic checkpoint, reflex churn), #AD-9 (arbiter / reflex fallback), #AD-14 (scheduler subsumes the tick), #Structural-Seed (`core/ … reflexes/`)]
- [Source: shelldon/core/state.py (`PersonalityState`, `WRITABLE_PATHS`, `PersistentState.apply_patch` — the API 3.2 calls)]
- [Source: shelldon/core/runtime.py (`_checkpoint_loop`/`_cleanup`/`__init__` interval guard — the background-task pattern to copy; `run()` single-consumer loop)]
- [Source: shelldon/core/arbiter.py:1-12 (single-consumer serial access — why no lock; the basis for AC2 single-writer serialization)]
- [Source: tests/test_state.py (`_DummySpawner`, `_await_true` predicate-poll, `tmp_path` checkpoint patterns to reuse)]
- [Source: tests/test_endurance_soak.py:143 (the `_bg` drains-to-0 invariant — why the reflex task needs its own slot)]
- [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-06-17.md (action #1 — prefer state-predicate asserts over sleep anchors)]
- [Source: _bmad-output/implementation-artifacts/3-1-persistent-personality-state-struct.md (the consumed API + the three review patches to pre-empt)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- `uv run pytest -q` → 173 passed, 2 skipped, 3 deselected (159 prior + 14 new reflex tests).
- `uv run lint-imports` → both contracts KEPT (core stays LLM-free; `core/reflexes.py` imports only `core/state` + stdlib `datetime`).
- Caught during verify: `_mark_interaction` makes state dirty on every inbound message, so the existing e2e/soak harnesses (which construct `Core` with the default checkpoint path) flushed real `~/.shelldon/state.json` on teardown. Fixed with an autouse conftest fixture that redirects `DEFAULT_CHECKPOINT_PATH` to `tmp_path` for every test; verified `~/.shelldon` is never created by a run.

### Completion Notes List

- **AC1** — `compute_reflex_patch(state, now)` (pure, in `core/reflexes.py`) drifts `mood.arousal` toward a time-of-day target (calm at night, lively midday) and, once idle past `IDLE_SETTLE_AFTER_S`, settles `mood.valence`/`energy` toward resting baselines — all gentle, hard-clamped nudges over the **existing** Story 3.1 closed paths. `Core._reflex_loop` applies it via `apply_patch` on an injectable interval, touching only `self.state` — no broker/spawner/Job (proven by `test_reflex_tick_mutates_state_offline` with a spawner that raises if touched). Returns `{}` when at rest, so an idle tick marks nothing dirty.
- **AC2** — the reflex tick and the turn-path `last_interaction` write both mutate through the one synchronous `apply_patch`; since `apply_patch` has no `await`, no mutation interleaves mid-write on the single core event loop. Verified the two writes coexist without clobbering (`test_turn_write_and_reflex_write_coexist_single_writer`).
- **AC3** — the reflex *policy* is a pure, deterministic function callable with no `Core`/tick (`test_reflex_function_is_deterministic_and_pure`); the *driver* owns only the interval. Epic 5's scheduler subsumes the tick by calling the same function as a reflex job — no behavior change, no forward dependency.
- **Composition with 3.1** — reflex writes mark the struct dirty; the existing 3.1 periodic checkpoint loop flushes it. 3.2 adds **no new disk write** (`test_reflex_tick_marks_dirty_for_the_311_flush`).
- **3.1 review hardening pre-empted** — `reflex_interval <= 0` rejected in `__init__`; `_reflex_loop` survives a transient tick error (logs + continues, `test_reflex_loop_survives_an_error`); the reflex task lives in its own `_reflex_task` slot, NOT `_bg` (preserves the 1.9 soak's "drains to 0" invariant).
- **Deviation — dropped the optional `last_tick` param.** The story signature suggested `compute_reflex_patch(state, now, last_tick)` with `last_tick` as "and/or"; the idle signal comes cleanly from `state.last_interaction` and drift is a per-tick fraction, so `last_tick` was unused. Omitted it to avoid a dead parameter (the function stays the subsumable unit AC3 requires).
- **Test-infra addition** — autouse `_isolate_state_checkpoint` fixture in `tests/conftest.py` (see Debug Log) so no test writes real `$HOME`.
- **Scope held** — no face/blink rendering or mood→face mapping (3.3), no scheduler (5.1), no new state fields / closed paths, no `contracts/` change, no arbiter change, no LLM/network in the tick.

### File List

- `shelldon/core/reflexes.py` (new) — pure `compute_reflex_patch(state, now)` reflex policy + tunable constants/clamps.
- `shelldon/core/runtime.py` (modified) — `reflex_interval` (injectable, positive-guarded); `_reflex_task` started in `run()`, cancelled in `_cleanup()`; `_reflex_loop`/`_reflex_tick`; `_mark_interaction` called on each inbound message.
- `tests/test_reflexes.py` (new) — 14 tests across AC1/AC2/AC3 + interval guard + loop-survives-error.
- `tests/conftest.py` (modified) — autouse fixture redirecting the default checkpoint path to `tmp_path` (no test writes real `$HOME`).

### Review Findings

- [x] [Review][Patch] `_await_true` and `_DummySpawner` copy-pasted verbatim from `tests/test_state.py` — story spec (Task 4) explicitly said "Reuse the `_DummySpawner` / predicate-poll patterns from `tests/test_state.py`"; both belong in `tests/conftest.py` so an interface change fails loudly in one place [`tests/test_reflexes.py:24,45`] — RESOLVED: moved `await_true` + `DummySpawner` to `tests/conftest.py`; both `test_state.py` and `test_reflexes.py` import them (local copies deleted).
- [x] [Review][Patch] `_idle_seconds` swallows unparseable `last_interaction` silently — a bad checkpoint value produces `None` with no log warning; `datetime.fromisoformat` also raises `TypeError` (not just `ValueError`) for non-string input; fix: add `log.warning(...)` in the except branch and broaden to `except (ValueError, TypeError)` [`shelldon/core/reflexes.py:65`] — RESOLVED: wraps parse+subtract in one try, `except (ValueError, TypeError)` logs a warning and returns None (`test_unusable_last_interaction_is_ignored_not_raised`).
- [x] [Review][Patch] WHAT comments on self-evident constants violate CLAUDE.md ("don't explain what the code does") — `_VALENCE_RANGE`, `DRIFT_RATE`, `IDLE_SETTLE_AFTER_S`, `RESTING_VALENCE`, `RESTING_ENERGY` comments restate the identifier in prose; the EPSILON "keeps the 3.1 flush idle when idle" line is the only justified WHY and should stay [`shelldon/core/reflexes.py:20-35`] — RESOLVED: removed the restating comments; kept (and expanded) the EPSILON WHY.
- [x] [Review][Consider] EPSILON baked into `compute_reflex_patch` reduces AC3 subsumability — the pure function now suppresses sub-threshold changes before returning, so Epic 5's scheduler cannot distinguish "at target" from "epsilon-suppressed"; the right layer is `Core._reflex_tick` (where the `if patch:` guard already exists) or `PersistentState.apply_patch` [`shelldon/core/reflexes.py:29`] — DECLINED (with rationale): keeping EPSILON in the pure policy makes the scheduler subsuming it inherit the same no-churn behavior unchanged — that is *more* subsumable, not less. The threshold is also an NFR7 (SD-wear) decision best made where the deltas are computed; moving it to the driver would lose NFR7 protection or duplicate the state-comparison. Added a clarifying comment at the constant.
- [x] [Review][Consider] Naive-tzinfo guard in `_idle_seconds` is an unreachable dead path — `_mark_interaction` always stores a tz-aware ISO string, so `last.tzinfo is None` never fires in production; either add a test proving the branch is needed, or remove it and document the UTC-only assumption [`shelldon/core/reflexes.py:67`] — RESOLVED: removed the dead naive-tz branch; a tz-naive value now falls through the broadened `except` (aware−naive raises `TypeError`) → treated as 'no idle signal' + warned, with the UTC-only assumption documented in the docstring (covered by the new test's naive case).

## Change Log

| Date       | Change                                                                 |
|------------|------------------------------------------------------------------------|
| 2026-06-17 | Implemented Story 3.2: resident reflex loop (pure `compute_reflex_patch` policy + in-core periodic tick applying it via 3.1's `apply_patch`; time-of-day arousal drift + idle settling; turn-path `last_interaction` write). All ACs met; 173 tests green; contracts KEPT; no real `$HOME` writes. |
| 2026-06-17 | Addressed code review: 4 findings resolved (shared test helpers → conftest; `_idle_seconds` logs + catches `TypeError`; removed WHAT-comments; removed dead naive-tz branch), 1 declined with rationale (EPSILON stays in the pure policy — more subsumable + NFR7). +1 test; 174 green; contracts KEPT. |
