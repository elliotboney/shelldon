---
baseline_commit: d2a71d83b34c99425a067f6ffd9c28f31dbb0c4c
---
# Story 7.0: Extract the turn-dispatch seam out of `core/runtime.py`

Status: done

<!-- Retro-born prep story (Epic 6 retro action #3, 2026-06-19). NOT in epics.md — it gates Epic 7. -->
<!-- Icebox trigger "Epic 7 story 1 starts" is HIT. Owner decision (2026-06-19): make the extract its OWN story/commit BEFORE 7.1 feature work, not folded in. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer about to add plugin-event wiring to core,
I want the arbiter-gated turn-dispatch seam (+ its prompt builders) pulled out of the `Core` god-object into its own `core/dispatch.py` module, with **zero behavior change**,
so that Epic 7's plugin events land on a clean, single-responsibility seam instead of piling onto a `runtime.py` that already hosts the bus loop, the turn lifecycle, four jobs, three prompt builders, the dispatch gate, and the single-writer apply path.

**Why this is a prep story (gates 7.1):** The Epic 5 icebox said "extract the scheduler driver + dispatch when Epic 6's dream grows `runtime.py` again." It did (Epic 6 retro: trigger HIT). `Core` now couples reflex + checkpoint + proactive + dream jobs, the prompt builders, the arbiter gate, and the turn lifecycle in one ~730-line class. Epic 7 plugin events (`message-answered`/`tool-used`/`day-alive`) will also touch core. Splitting the dispatch concern out **before** that is the same "harden the seam, then build on it" discipline as Story 5.0 — and it is a pure, behavior-preserving refactor with a hard success criterion: **the full suite stays green at the current count with zero test-logic changes.**

**This is REFACTOR-ONLY.** No feature, no new contract, no behavior change. If you find yourself "improving" the dispatch logic, stop — that is out of scope.

## Acceptance Criteria

### AC1 — A new `core/dispatch.py` owns the turn-dispatch seam

**Given** the dispatch concern today lives as private methods on `Core` (`shelldon/core/runtime.py`)
**When** the extract is done
**Then** a new module `shelldon/core/dispatch.py` defines a `TurnDispatcher` class that owns these four methods, moved **verbatim** (body unchanged):
- `_dispatch_turn_job(job)` (`runtime.py:550-591`) — the arbiter + cooldown + budget admit gate (Story 5.2)
- `_resolve_job_prompt(job)` (`runtime.py:593-616`) — builder-or-static prompt resolution
- `_build_proactive_prompt()` (`runtime.py:525-533`) — the Story 5.4 proactive `prompt_builder`
- `_build_dream_prompt()` (`runtime.py:535-541`) — the Story 6.2 dream `prompt_builder`

**And** `core/dispatch.py` follows the established policy/driver naming split (cf. `core/budget.py`, `core/power.py`, `core/reflexes.py`): `dispatch.py` is the **driver** half whose policy halves (`budget`, `power`) already exist.

### AC2 — The dispatcher is wired by injection, not inheritance, and the admit critical section stays await-free

**Given** the moved methods read `self.arbiter`, `self._budget`, `self.state`, `self.faces`, `self.history`, and call `self._start_turn` (the turn lifecycle that STAYS on `Core`)
**When** `TurnDispatcher` is constructed in `Core.__init__`
**Then** its dependencies are passed in (matching the existing injection style — `Scheduler` already takes `now`/`dispatch_turn`/`power`/`backoff` callables at `runtime.py:239-244`):
- the shared refs it reads: `arbiter`, `budget`, `state`, `faces`, `history`
- a `start_turn` **callback** = `core._start_turn` (the lifecycle stays on `Core`; the dispatcher calls back into it)

**And** the `_dispatch_turn_job` admit sequence (`is_idle` check → `apply_patch` → `arbiter.submit`, `runtime.py:563-565`) remains **await-free up to `_start_turn`** — the no-lock single-critical-section invariant must survive the move byte-for-byte. The only `await` is the trailing `start_turn(...)` callback.

### AC3 — `Core` wires the dispatcher at composition; `_scheduler_loop` STAYS on `Core`

**Given** `Core.__init__` today wires `dispatch_turn=self._dispatch_turn_job` (`runtime.py:241`) and registers the proactive/dream jobs with `prompt_builder=self._build_proactive_prompt` / `self._build_dream_prompt` (`runtime.py:262`, `281`)
**When** the dispatcher is introduced
**Then** `Core.__init__` constructs `self._dispatcher = TurnDispatcher(...)`, and the scheduler/job wiring points at the dispatcher's methods (`dispatch_turn=self._dispatcher.dispatch_turn_job`, `prompt_builder=self._dispatcher.build_proactive_prompt`, etc.).

**And** `_scheduler_loop` (`runtime.py:479-501`) and `_last_interaction_dt` (`runtime.py:503-523`) **STAY on `Core`** — they are the resident scheduler task launched in `run()` (`runtime.py:296`) and cancelled in `_cleanup()` (`runtime.py:715`); moving a resident-task driver would disturb the 1.9 soak's "`_bg` drains to 0" invariant and three `test_reflexes.py` callsites for no benefit. (Scope-limit is deliberate — see Out of scope.)

### AC4 — Existing tests pass UNCHANGED via thin `Core` delegators

**Given** `tests/test_turn_dispatch.py` calls these as bound methods on a constructed `Core` (24 references: `await core._dispatch_turn_job(...)` at lines 63/86/102/118/133/148/170/213/232/250/331/377/389/398, and `core._build_dream_prompt()` at line 365)
**When** the methods move to `TurnDispatcher`
**Then** `Core` keeps **thin delegators** (`async def _dispatch_turn_job(self, job): return await self._dispatcher.dispatch_turn_job(job)`, and likewise `_build_dream_prompt`, `_build_proactive_prompt`, `_resolve_job_prompt`) so **every existing test callsite passes with ZERO test-logic edits**.

**And** the full suite is green at the current count (**431 test functions / the project's reported 448 pass+soak baseline**) with no test assertions changed. This is the gate: a refactor that needs a test edited is not behavior-preserving — investigate before editing a test.

### AC5 — The `budget.py` driver pointer is corrected (the one allowed doc touch)

**Given** `core/budget.py:7-8` documents "The driver (apply the patch + admit through the arbiter + spawn) lives in `core/runtime.py` — same policy/driver split as `core/reflexes.py`."
**When** the driver moves
**Then** that one docstring line is updated to point at `core/dispatch.py`. (This is the only comment edit in scope — it would otherwise be a stale reference your own change created.)

### Out of scope (explicit — do NOT do here)

- **Moving `_scheduler_loop` / `_last_interaction_dt`** — they stay on `Core` (AC3). Resident-task lifecycle; not the dispatch concern.
- **Migrating the `test_turn_dispatch.py` callsites** to `core._dispatcher....` and deleting the delegators — optional polish, a SEPARATE later commit if ever. The delegators read cleanly; leave them. (If you do it, it must be its own commit with the suite green at each step.)
- **Any change to the turn lifecycle** (`_start_turn`/`_handle_result`/timeout/reap), the reflex jobs, the single-writer apply path (`_apply_proposed_ops`/`apply_memory_op`/`apply_add_face`), or the emit helpers — all STAY on `Core`, untouched.
- **Any plugin-host, manifest, region, event, or broadcast code** — that is Story 7.1+. This story ships no new contract, no new `MsgKind`, no `Region` member.
- **"Improving" the dispatch logic, renaming the budget/cooldown vocabulary, or touching the no-lock invariant** — verbatim move only.

## Tasks / Subtasks

- [x] **Task 1 — Create `shelldon/core/dispatch.py` with `TurnDispatcher`** (AC1, AC2)
  - [x] New module + class. Constructor injects `arbiter`, `budget`, `state`, `faces`, `history`, and a `start_turn` callback (keyword-only, matching the codebase's injection style).
  - [x] Move `_dispatch_turn_job` → `dispatch_turn_job(job)` **verbatim**; the trailing call becomes `await self._start_turn(prompt, record_owner_text=job.history_owner_text)` where `self._start_turn` is the injected callback. Everything else byte-for-byte (the await-free admit section preserved verbatim, comment included).
  - [x] Move `_resolve_job_prompt` → `resolve_job_prompt(job)`, `_build_proactive_prompt` → `build_proactive_prompt()`, `_build_dream_prompt` → `build_dream_prompt()` verbatim. The pure policy fns `build_proactive_prompt`/`build_dream_prompt` are now imported in `dispatch.py` (moved off `runtime.py` with the wrappers).
  - [x] Carried imports (`datetime`/`UTC`, `Decision`, `Job`, `logging`) into `dispatch.py`; removed the now-orphaned `Decision` + `build_dream_prompt`/`build_proactive_prompt` imports from `runtime.py`.
  - [x] verify: full suite green; `import shelldon.core.dispatch` clean (no import cycle — `dispatch` imports `budget`/`proactive`/`scheduler`, all of which `runtime` already imports before it).
- [x] **Task 2 — Wire the dispatcher in `Core.__init__`** (AC3)
  - [x] Construct `self._dispatcher = TurnDispatcher(arbiter=self.arbiter, budget=self._budget, state=self.state, faces=self.faces, history=self.history, start_turn=self._start_turn)` after the shared refs exist, just before the `Scheduler(...)` construction.
  - [x] Repointed `Scheduler(dispatch_turn=self._dispatcher.dispatch_turn_job, ...)` and the proactive/dream `prompt_builder=self._dispatcher.build_proactive_prompt` / `build_dream_prompt` wiring.
  - [x] verify: `uv run pytest tests/test_turn_dispatch.py tests/test_reflexes.py` green (within the full run).
- [x] **Task 3 — Add thin `Core` delegators so existing tests pass unchanged** (AC4)
  - [x] On `Core`: `_dispatch_turn_job`, `_resolve_job_prompt`, `_build_proactive_prompt`, `_build_dream_prompt` are now one-line delegates to `self._dispatcher.*`, names/signatures unchanged.
  - [x] verify: `uv run pytest -q` — FULL suite **458 passed, 3 skipped, 5 deselected**, identical to baseline, **zero test files edited** (`git status` confirms 0 `tests/` changes).
- [x] **Task 4 — Correct the one stale driver pointer** (AC5)
  - [x] `core/budget.py`: driver pointer `core/runtime.py` → `core/dispatch.py`.
- [x] **Task 5 — Import-linter + final gate**
  - [x] verify: `uv run lint-imports` — `core is LLM-free (AD-1) KEPT`, `transport ... KEPT` (2 kept, 0 broken). `dispatch.py` is in `shelldon.core` and imports no provider lib.
  - [x] verify: `uv sync --locked` clean (0 new deps), `uv run lint-imports` KEPT, `uv run pytest -q` green. Full CI shape passes.

## Dev Notes

### The exact extract boundary (from a full read of `runtime.py` + `scheduler.py` + `proactive.py`)

**Moves to `core/dispatch.py` (`TurnDispatcher`):**

| Method (current) | Lines | New name | Reads / calls |
|---|---|---|---|
| `_dispatch_turn_job` | `runtime.py:550-591` | `dispatch_turn_job` | `resolve_job_prompt`, `arbiter.is_idle`/`submit`, `budget.evaluate`/`admission_patch`, `state.state.budget`/`apply_patch`, **`start_turn` callback** |
| `_resolve_job_prompt` | `runtime.py:593-616` | `resolve_job_prompt` | `job` only (pure given the builders) |
| `_build_proactive_prompt` | `runtime.py:525-533` | `build_proactive_prompt` | `state.state`, `faces.select`, → `proactive.build_proactive_prompt` |
| `_build_dream_prompt` | `runtime.py:535-541` | `build_dream_prompt` | `history.pending_learnings`, → `proactive.build_dream_prompt` |

**Stays on `Core` (do not touch):** `run`, `_start_turn`, `_handle_result`, `_arm_timeout`/`_disarm_timeout`/`_timeout_watch`/`_await_reap`, all emit helpers (`_send_reply`/`_push_face`/`_degrade`/`_record_turn`/`_next_seq`), the reflex jobs (`_run_reflex_job`/`_reflex_tick`/`_maybe_push_mood_face`/`_run_checkpoint_job`/`_checkpoint_if_dirty`), the apply path (`apply_add_face`/`apply_memory_op`/`_apply_proposed_ops`/`_mark_interaction`), bookkeeping (`_track`/`_cleanup`), **and `_scheduler_loop` + `_last_interaction_dt`** (AC3).

### The one hard seam: `start_turn`

`_dispatch_turn_job` ends with `await self._start_turn(prompt, record_owner_text=job.history_owner_text)` (`runtime.py:591`). `_start_turn` is the turn lifecycle and STAYS on `Core`. Inject it as a callback (`start_turn=self._start_turn`) — a bound method, so behavior is byte-identical. This is the established pattern: `Scheduler` already receives `dispatch_turn=self._dispatch_turn_job` as an injected callable (`runtime.py:241`). Do NOT pass the whole `Core` in and call back — inject the single callable (option [A] from the scoping analysis; tightest coupling that works).

### The load-bearing invariant to preserve verbatim

The admit section of `_dispatch_turn_job` is **await-free** between the `is_idle` check and `arbiter.submit` (`runtime.py:563-565` comment: "the admit sequence … has NO `await`, so it is atomic w.r.t. the consumer; the arbiter's no-lock single-critical-section invariant holds"). The scheduler runs as a sibling task to `run()`. If the move introduces any `await` in that window, you have changed behavior and broken the concurrency invariant. Move the body byte-for-byte; the only `await` is the trailing `start_turn`.

### Why a delegator (not a test rewrite) is the right call

`test_turn_dispatch.py` constructs `Core(sock_path, spawner, checkpoint_path=...)` across 21 test functions and calls `core._dispatch_turn_job(...)` (24 refs) + `core._build_dream_prompt()` (1 ref). Keeping thin `Core` delegators means the refactor is provable with **zero test edits** — the cleanest possible bisect line between "refactor" and "feature." A refactor that forces a test change is, by definition, not behavior-preserving; if a test goes red, the move was wrong, not the test.

### Source tree components to touch

- **CREATE:** `shelldon/core/dispatch.py`
- **MODIFY:** `shelldon/core/runtime.py` (move 4 methods out, add 4 thin delegators, construct + wire `TurnDispatcher`, drop orphaned imports), `shelldon/core/budget.py` (one docstring line)
- **DO NOT MODIFY:** any `tests/` file, `contracts/`, `scheduler.py`, `proactive.py`, `app.py`

### Testing standards summary

- Run via `uv run pytest -q`; `addopts = -m 'not live'` already keeps the default run offline (no network, no live key needed). Soak: `-m soak`. Live: opt-in `-m live` (not relevant here).
- Success = the existing suite green at the baseline count with **no test-logic changes**. New tests are NOT required for a behavior-preserving move (the existing 24+ dispatch tests already cover the moved code through the delegators) — add one only if you want a direct `TurnDispatcher` construction test, but it is optional and must not be the thing that makes the story "pass."
- `uv run lint-imports` must stay green: `dispatch.py` lives in `shelldon.core`, inheriting the AD-1 "core is LLM-free" forbidden-import contract — it imports no provider SDK, so it's clean.

### Project Structure Notes

- `core/dispatch.py` sits alongside `budget.py`/`power.py`/`reflexes.py` — the policy/driver convention the codebase already uses. No new package, no new contract, no `SCHEMA_VERSION` touch.
- Naming: `dispatch.py` (not `turn_jobs.py`) — the reflex jobs are also "jobs" but stay on `Core`, so "dispatch" is the precise boundary word.

### References

- [Source: _bmad-output/implementation-artifacts/epic-6-retro-2026-06-19.md#Action items] — action #3 (extract trigger HIT, due before Epic 7) and the safe-slice already done (dream directive → `build_dream_prompt`).
- [Source: _bmad-output/implementation-artifacts/sprint-status.yaml#icebox] — `runtime-dispatch-extract: deferred … Trigger: Epic 7 story 1 starts.`
- [Source: shelldon/core/runtime.py:525-616] — the four methods to move.
- [Source: shelldon/core/runtime.py:239-285] — the scheduler + job wiring to repoint.
- [Source: shelldon/core/budget.py:1-12] — the policy/driver split convention + the stale driver pointer (AC5).
- [Source: tests/test_turn_dispatch.py] — the 24 `core._dispatch_turn_job` / `core._build_dream_prompt` callsites the delegators preserve.
- [Source: _bmad-output/implementation-artifacts/5-0-resilience-hardening-prep.md] — the prep-story precedent (retro-born, gates the next epic, not in epics.md).

### Review Findings

- [x] [Review][Fixed] `.strip()` called outside the `try` block — a truthy non-str return from `prompt_builder` (e.g. `list`, `int`) causes unguarded `AttributeError` [dispatch.py] — **FIXED 2026-06-19** (owner chose fix-in-7-0): `.strip()` moved inside the `try`, so a non-str return is caught → skip. Test: `test_non_str_builder_return_skips_not_crashes`.
- [x] [Review][Fixed] `apply_patch` raises after `arbiter.submit` reserves slot → slot permanently wedged [dispatch.py] — **FIXED 2026-06-19** (owner chose fix-in-7-0): `apply_patch` wrapped in try/except → `arbiter.reset()` before re-raise, so the reserved slot can't leak. Test: `test_slot_released_when_recording_the_spend_fails`.
- [x] [Review][Defer] `pending_learnings()` row missing key → unguarded `KeyError` in `build_dream_prompt` comprehension [dispatch.py:59-60] — deferred, pre-existing (runtime.py:540-541)
- [x] [Review][Defer] `faces.select` raises → caught by `resolve_job_prompt` try/except but logged as misleading "builder failed" [dispatch.py:51] — deferred, pre-existing (runtime.py:532)
- [x] [Review][Defer] No-await invariant in `dispatch_turn_job` admit section asserted only in comment — no static enforcement or test [dispatch.py:62-103] — deferred, pre-existing pattern
- [x] [Review][Defer] `_start_turn` bound-method injection ordering undocumented — fragile if `Core.__init__` order ever changes — deferred, pre-existing pattern (Scheduler has same pattern)

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Baseline (pre-change): `uv run pytest -q` → 458 passed, 3 skipped, 5 deselected.
- Post-change gate: `uv run pytest -q` → 458 passed, 3 skipped, 5 deselected (identical); `uv run lint-imports` → 2 contracts KEPT, 0 broken; `uv sync --locked` → 0 dep changes; `git status --short` → 0 files under `tests/` changed.

### Completion Notes List

- **Behavior-preserving extract done as scoped.** Moved `_dispatch_turn_job`, `_resolve_job_prompt`, `_build_proactive_prompt`, `_build_dream_prompt` out of `Core` into a new `TurnDispatcher` (`shelldon/core/dispatch.py`). The admit critical section (`is_idle` → `apply_patch` → `submit`) moved byte-for-byte; the only `await` remains the trailing `start_turn` callback (the no-lock single-critical-section invariant is preserved).
- **Injection over inheritance (AC2).** `TurnDispatcher` holds the injected collaborators under the same attribute names `Core` used (`self.arbiter`/`self.state`/`self.faces`/`self.history`/`self._budget`) so the method bodies are verbatim; `start_turn=self._start_turn` is the callback into the lifecycle that stays on `Core`.
- **`_scheduler_loop` + `_last_interaction_dt` stayed on `Core`** (AC3) — resident-task lifecycle untouched, so the 1.9 soak's `_bg`-drains-to-0 invariant and the 3 `test_reflexes.py` scheduler-loop callsites are undisturbed.
- **Thin delegators kept all 24 `test_turn_dispatch.py` callsites working with zero test edits** (AC4) — the clean bisect line between refactor and feature. No new test was required (the existing dispatch tests cover the moved code through the delegators); none was added.
- **Logger:** `dispatch.py` gets its own `shelldon.core.dispatch` logger (conventional). Verified no test asserts on logger name, so this is not a behavior change.
- **Orphan cleanup limited to what this change orphaned:** removed `Decision` and the two `build_*_prompt` imports from `runtime.py` (now used only by `dispatch.py`). `BudgetGate`, `datetime`/`UTC`, `Job` remain (still used).
- **AC5:** corrected the one stale driver pointer in `budget.py` (`core/runtime.py` → `core/dispatch.py`).
- No new contract, no `MsgKind`/`Region` change, no new dependency. Pure structural prep for Epic 7.

### File List

- `shelldon/core/dispatch.py` — NEW. `TurnDispatcher` (the dispatch driver: `dispatch_turn_job`, `resolve_job_prompt`, `build_proactive_prompt`, `build_dream_prompt`).
- `shelldon/core/runtime.py` — MODIFIED. Added `TurnDispatcher` import + construction in `__init__`; repointed scheduler `dispatch_turn` + proactive/dream `prompt_builder` wiring; replaced the 4 moved methods with thin delegators; removed orphaned `Decision`/`build_*_prompt` imports.
- `shelldon/core/budget.py` — MODIFIED. One docstring line: driver pointer → `core/dispatch.py`.
- `tests/test_turn_dispatch.py` — MODIFIED (post-review hardening). +2 regression tests: `test_slot_released_when_recording_the_spend_fails` (review #1), `test_non_str_builder_return_skips_not_crashes` (review #2).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `epic-7 → in-progress`, `7-0 → review → done`, icebox `runtime-dispatch-extract → scheduled`.

### Change Log

- 2026-06-19 — Story 7.0 implemented: extracted the turn-dispatch seam from `core/runtime.py` into `core/dispatch.py` (`TurnDispatcher`), behavior-preserving. Suite 458 passed (baseline-identical), import-linter KEPT, 0 test edits, 0 new deps. Status → review.
- 2026-06-19 — Post-review hardening (owner chose fix-in-7-0): fixed review findings #1 (arbiter slot leak when `apply_patch` raises → reset-before-reraise) and #2 (non-str builder return → `.strip()` inside `try` → skip not crash). +2 regression tests. Suite 460 passed, import-linter KEPT, 0 new deps. Findings #3–#6 remain deferred (cosmetic/test/doc, pre-existing). Status → done.
