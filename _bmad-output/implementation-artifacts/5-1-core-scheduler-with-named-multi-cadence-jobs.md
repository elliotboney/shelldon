---
baseline_commit: cf0c1b3244fbbbf4c3be742ed112dff0db4bf305
---

# Story 5.1: Core scheduler with named multi-cadence jobs

Status: done

<!-- First feature story of Epic 5. Gated by Story 5.0 (resilience hardening) — done. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet's background behaviors to run on independent schedules,
so that mood drift, reflection, and checks each fire at the right cadence — not all gated behind one slow heartbeat.

**Why now / what it unblocks:** this is the spine of Epic 5 (AD-14, "the autonomous mind"). It replaces v1's single heartbeat with **named jobs, each on its own cadence and cost tier**. 5.0 just made the turn lifecycle wedge-proof; 5.1 builds the engine that will *drive* turns autonomously — but in 5.1 only the no-LLM **reflex jobs** actually run. Cost-tier gating + credit budget for **turn jobs** is 5.2, battery-aware backoff is 5.3, proactive turns are 5.4.

## Acceptance Criteria

### AC1 — Named, multi-cadence, cost-tiered job registry

**Given** the core scheduler
**When** jobs are registered
**Then** each job has: a **name**, a **cadence** (one of `interval` / `cron`-style / `idle`-triggered), and a **cost tier** (`reflex` vs `turn`).

- Cadence kinds: `interval` (every N seconds), `cron`-style (calendar-ish — at minimum a daily/at-time trigger; a full cron grammar is NOT required, see Dev Notes), `idle`-triggered (fires after N seconds since the last owner interaction — the signal already lives in `state.last_interaction`).
- The registry is in-core, LLM-free (AD-1). Registration is explicit (the composition root / Core wires the built-in jobs); a general plugin-registration API is Epic 7, not here.

### AC2 — Epic 3's reflex tick becomes a reflex job; "heartbeat" is just one job

**Given** Epic 3's reflex tick (`_reflex_loop` / `_reflex_tick` + the between-turn mood-face push)
**When** the scheduler exists
**Then** the reflex drift runs as a cost-tier **`reflex`** job on the scheduler with **unchanged behavior** (same drift, same between-turn mood-face push, same no-LLM/offline-safe guarantee), and the old standalone `_reflex_loop` is gone — "heartbeat" is now one named job among many.

- **Unchanged behavior is the bar:** all of `test_reflexes.py` and the reflex assertions in the soak must still pass. The drift math, the `apply_patch` single-writer path, and `_maybe_push_mood_face` (push only between turns, only on token change) are preserved exactly — they move, they don't change.

### AC3 — Incoming messages/events bypass the scheduler (immediate, not cadence-gated)

**Given** an incoming message or event
**When** it arrives
**Then** it is handled **immediately**, bypassing the scheduler entirely — events are never queued behind a tick. The existing `run()` consumer (INBOUND_MSG → arbiter.submit → turn; RESULT → fence) is unchanged; the scheduler is a *parallel* in-core driver, not a gate in front of the inbox.

### Out of scope (explicit — later Epic 5 stories)

- **Cost-tier gating, cooldown, daily credit/turn budget** for turn jobs — **Story 5.2**. In 5.1 the cost tier is a registered *property*; turn-job *dispatch through the arbiter* is left as a seam 5.2 fills (do not run fork+LLM jobs on a cadence here).
- **Battery-aware backoff** (PiSugar2 → stretch/skip) — **Story 5.3**.
- **Proactive action** (greeting / mood-driven idle turn with no prompt) — **Story 5.4**.
- **Dreaming/reflection turn jobs** (AD-15) and `capture_learning` — **Epic 6**.
- **A plugin job-registration API** — Epic 7.

## Tasks / Subtasks

- [x] **Task 1 — The scheduler + job model (AC1)**
  - [x] Add `core/scheduler.py` (LLM-free): a `Job` (name, cadence, cost_tier, the callable/coro to run) and a `Scheduler` that owns a set of jobs and, on its tick, runs every job that is **due**. Cadence kinds: `interval`, `cron`-style (daily-at-time minimum), `idle` (due when `now - last_interaction ≥ N`). Cost tier is an enum `reflex | turn`.
  - [x] The scheduler computes due-ness from an injected `now`/clock + `state.last_interaction` (no real sleeps in the policy — testable deterministically, the reflex/checkpoint-loop pattern).
  - [x] Unit tests: each cadence kind fires when due and not before; a job's `cost_tier`/`name` round-trip; due-set computed correctly from a fixed clock.
- [x] **Task 2 — Run the scheduler as a single parkable in-core task (AC1, soak-safe)**
  - [x] Core owns ONE scheduler task in its own slot (`_scheduler_task` — NOT in `_bg`; see the soak invariant in Dev Notes). It ticks on a base interval and dispatches due jobs.
  - [x] **Parkable:** the scheduler's base cadence (`scheduler_interval`) and the reflex job's interval are injectable so the 1.9 soak parks them far out (`scheduler_interval=3600, reflex_interval=3600`) and keeps `_seq == 2*turns` exact. Cancels cleanly on teardown (`_cleanup`).
  - [x] Reflex-tier jobs execute in-core, no fork, no LLM, guarded best-effort (one bad job logs + the scheduler keeps ticking — `Scheduler.tick` per-job guard).
- [x] **Task 3 — Migrate the reflex tick to a reflex job (AC2)**
  - [x] Register the Epic 3 reflex drift (`_reflex_tick` + `_maybe_push_mood_face`) as a `reflex`-tier job named `"reflex"`; deleted the standalone `_reflex_loop`. Behavior unchanged.
  - [x] **Decision: folded** the periodic checkpoint flush into the scheduler as a second `reflex`-tier job `"checkpoint"` (the recommended option — deleted `_checkpoint_loop` so no orphan sibling loop remains).
  - [x] All of `test_reflexes.py` and the soak's reflex/`_seq` assertions still pass.
- [x] **Task 4 — Events still bypass (AC3)**
  - [x] Confirmed + tested that INBOUND_MSG/RESULT handling in `run()` is unchanged and not gated behind a scheduler tick (`test_inbound_message_bypasses_a_parked_scheduler`: a turn completes with the scheduler parked an hour out).
- [x] **Task 5 — Document the background-emitter rule (closes an iceboxed chore)**
  - [x] Wrote the rule into `test_endurance_soak.py` (the `soak-background-emitter-doc` chore): any new core background emitter must live in its own slot (not `_bg`) and be parkable or the `_seq`/`_bg` invariants break. Parked the scheduler in both soak constructions.
- [x] **Task 6 — Full-suite + contracts**
  - [x] Full `pytest` green (338 passed) incl. the soak (2 passed: `_seq == 2N`, `_bg` drains); both import-linter contracts **KEPT** (scheduler in `core/`, LLM-free); no new real-`$HOME` write paths.

## Dev Notes

**This story generalizes existing in-core interval loops into a named-job scheduler — it does not invent autonomy yet.** The hard part is doing it *without changing reflex behavior* and *without breaking the 1.9 soak's exact accounting*. Read the existing loops and the soak before writing code.

### The code being generalized (read these first — `shelldon/core/runtime.py`)

- `_reflex_loop` (the `while True: sleep(reflex_interval); _reflex_tick(); _maybe_push_mood_face()` task) — a **singleton long-lived task in its own slot** (`self._reflex_task`), guarded so one bad tick logs + keeps ticking, cancelled in `_cleanup`. **This becomes a reflex job.**
- `_checkpoint_loop` (same shape, `self._checkpoint_task`, flushes state if dirty on `checkpoint_interval`, NFR7) — the comments already say "the seam Story 3.2's reflex tick and Epic 5's scheduler subsume." **Candidate second reflex job (Task 3 decision).**
- `_reflex_tick` (pure: `compute_reflex_patch` → single-writer `apply_patch`, skip no-op) and `_maybe_push_mood_face` (push the mood face ONLY when `fence.is_idle and arbiter.is_idle`, only on token change) — **move verbatim into the reflex job.**
- `run()` consumer loop (INBOUND_MSG → `arbiter.submit` → `_start_turn`; RESULT → `_handle_result`) — **AC3: leave unchanged.** The scheduler is a sibling task created in `run()` alongside `_checkpoint_task`/`_reflex_task`, not a layer over `core_inbox`.

### CRITICAL: the 1.9 soak background-emitter invariant (the iceboxed rule)

`tests/test_endurance_soak.py` asserts, after N turns:
- `h.core._seq == 2 * SOAK_TURNS` — **exactly** two face pushes per turn (thinking + reply), nothing else. A mood-face push from a reflex tick would perturb this, so the soak **parks** the reflex by constructing `Core(..., reflex_interval=3600)`.
- `len(h.core._bg) == 0` drains — `_bg` holds only transient per-turn reap tasks; a **permanent resident** (a long-lived scheduler task) placed in `_bg` would break the "drains to 0" invariant.

**Therefore the scheduler MUST:** (1) live in its **own slot** (`self._scheduler_task`), never `_bg`; (2) be **parkable** — expose an injectable base cadence (and keep `reflex_interval` injectable) so the soak can push them out of the measurement window and keep `_seq` exact. When you add the scheduler, update the soak's construction to park it the same way it parks `reflex_interval` today. This is the `soak-background-emitter-doc` chore (Task 5) — write the rule into the soak.

### Cadence design (keep it minimal — AD-14, not a cron library)

- `interval`: due when `now - last_run ≥ period`. (Covers reflex + checkpoint.)
- `idle`: due when `now - state.last_interaction ≥ period`. The idle signal already exists — `_mark_interaction()` writes `state.last_interaction` on every owner message.
- `cron`-style: **minimum viable** — a daily "at HH:MM" / once-per-calendar-day trigger is enough for AD-14's named jobs (dreaming runs nightly, Epic 6). **Do NOT** pull in a cron-grammar dependency or build a full 5-field parser — that's speculative (a senior engineer would call it overcomplicated). A small "due once per day after time T" is sufficient; note the limitation.
- Inject the clock (`now`) so due-ness is unit-testable without sleeping (mirror how the reflex/checkpoint loops are tested with injected intervals).

### Turn-tier jobs: register, don't run (the 5.2 seam)

AC1 requires a `turn` cost tier to EXIST as a registered property. AC2 only requires **reflex** jobs to actually execute. **Do not** dispatch fork+LLM turn jobs on a cadence in this story — leave a clear seam (e.g. the scheduler collects due `turn`-tier jobs and hands them to a dispatch hook that 5.2 implements with the cooldown + daily credit/turn budget + the arbiter gate). Per AD-14: "Scheduler-proposed turn jobs go through the **arbiter** (AD-9) — same ≤1-worker bound, coalescing, and credit/battery gate as every other turn; **the scheduler never forks directly**." So when 5.2 wires it, a due turn job becomes an `arbiter.submit`-style request, NOT a direct `spawn_turn`. Build the seam pointing that way; don't implement the gate here.

### Architecture invariants (binding)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-14** (lines 137-140): "a **core-resident scheduler** owns the pet's self-driven life as **named jobs**, each with its own **cadence** — `interval`, `cron`-style, or `idle`-triggered — replacing v1's single heartbeat (**heartbeat is now just one job**). Every job is tagged by **COST TIER**: **reflex jobs** … run in-core, no LLM, cheap CPU; **turn jobs** … each cost a fork+LLM, are few, cooldown-gated, and draw on a daily credit/turn BUDGET (AD-9). … **Incoming messages/events bypass the scheduler** entirely. Scheduler-proposed turn jobs go through the **arbiter** (AD-9) … the scheduler never forks directly."
- **AD-9** (lines 112-115): the arbiter is the single gate for turns (≤1 in flight, coalesce, degrade-to-reflex). Turn jobs admitted by the scheduler are gated here — 5.2.
- **AD-1**: `core/` is LLM-free (import-linter). The scheduler lives in `core/` and imports no provider/worker code.
- **AD-5**: reflex jobs mutate `state` only through the single-writer `apply_patch`.

### Testing standards

- `pytest`; **deterministic clock injection, never `asyncio.sleep` anchors** for due-ness (Epic 2 retro #1 / the reflex-loop test pattern). Use the shared `await_true` for the integration-level checks.
- Preserve `test_reflexes.py` verbatim-behavior; extend it or add `tests/test_scheduler.py` for the job/cadence model.
- **Apply `dev-loop-checklist.md`** before review: best-effort guards on every job run; tests assert real values + exercise the not-due / bad-job branches; no false-positive masking; conftest isolation (the scheduler touches only `state`, already isolated).
- Run the **soak** (`-m soak`) locally — it's the regression that catches a background-emitter mistake.

### Project Structure Notes

- New: `shelldon/core/scheduler.py`, `tests/test_scheduler.py`. Modified: `shelldon/core/runtime.py` (own a `_scheduler_task`, register the reflex [+ checkpoint] job, delete `_reflex_loop` [and maybe `_checkpoint_loop`], extend `_cleanup`), `tests/test_endurance_soak.py` (park the scheduler + the background-emitter-rule note).
- LLM-free core (AD-1) must stay **KEPT**.

### Previous-story intelligence (Story 5.0 — done)

- 5.0 hardened the turn lifecycle (coherent timeouts W<R<T, the ≤1 slot always releases, reap awaited before the arbiter frees). **5.1's future turn jobs ride that now-safe path** — but 5.1 doesn't dispatch them (5.2 does), so 5.1 doesn't touch the turn lifecycle.
- 5.0's discipline carries: guarded best-effort everywhere, tests on failure branches, no scope creep. The reflex/checkpoint loops already follow the "log + keep ticking" guard — keep it when they become jobs.
- **The soak is the canary:** 5.0 kept `_seq`/`_bg` exact; 5.1 adds the first new resident emitter since then, so the soak's parkability is the thing most likely to break. Wire it in the same change (the iceboxed rule).

### References

- [Source: `_bmad-output/planning-artifacts/epics.md`#Story-5.1] — the three ACs verbatim.
- [Source: ARCHITECTURE-SPINE.md] AD-14 (137-140), AD-9 (112-115), AD-1, AD-5.
- [Source: `shelldon/core/runtime.py`] `_reflex_loop`/`_checkpoint_loop`/`_reflex_tick`/`_maybe_push_mood_face`/`run`/`_cleanup` — the code generalized here.
- [Source: `tests/test_endurance_soak.py`] the `_seq == 2N` + `_bg` drains invariants + the `reflex_interval=3600` park (the background-emitter rule).
- [Source: sprint-status.yaml icebox] `soak-background-emitter-doc` — closed by Task 5.

## Review Findings

*(Code review 2026-06-18 — 3 layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor)*

### Patches

- [x] [Review][Patch] `_scheduler_loop` does not guard `tick()` — exceptions from `self._now()` or `Cadence.is_due()` bypass the per-job guard and kill `_scheduler_task` silently with no restart [`runtime.py:369`] — **FIXED**: wrapped `tick()` in a `try/except Exception + log` inside the loop (CancelledError still propagates); one bad tick can no longer kill the resident task.
- [x] [Review][Patch] `float('nan')` passes the `<= 0` guard in `Interval.__init__` and `Idle.__init__` — fires only on first tick then silently never fires again [`scheduler.py:53,69`] — **FIXED**: both guards now use `not (period_s > 0)`, which rejects NaN as well as zero/negative; +NaN rejection tests.
- [x] [Review][Patch] `Daily.__init__` does not validate that `at` is a tz-naive `time` — a tz-aware `time` causes `TypeError` in `is_due()` propagating unguarded [`scheduler.py:89`] — **FIXED**: `Daily.__init__` rejects a tz-aware `at` at construction (fail fast); +rejection test.
- [x] [Review][Patch] Missing test: failing reflex job must still have `last_run` set before the failure (so it waits its period before retry — never busy-loops); currently implicit, not asserted [`tests/test_scheduler.py`] — **FIXED**: `test_failing_job_advances_last_run_so_it_does_not_busy_loop`.
- [x] [Review][Patch] Missing test: `dispatch_turn=None` with a due TURN job — verify the job is marked run, not dispatched, and the scheduler keeps ticking [`tests/test_scheduler.py`] — **FIXED**: `test_due_turn_job_without_a_dispatch_hook_is_skipped_not_run_in_core`.

### Deferred

- [x] [Review][Defer] `Idle` cadence silently never fires in production — `_scheduler_loop` always passes `last_interaction=None`; code comment correctly defers wiring to Story 5.4 but a deferred-work entry ensures it isn't forgotten [`runtime.py:366-369`] — deferred, intentional 5.4 seam
- [x] [Review][Defer] `Cadence` base class raises `NotImplementedError` rather than using `abc.ABC` — a forgotten `is_due()` override only fails at runtime, not at instantiation [`scheduler.py:44`] — deferred, cosmetic/future-proofing
- [x] [Review][Defer] `Daily` cadence has no guard against clock jumps — a backward NTP correction suppresses the daily job for up to 24h silently [`scheduler.py:92-95`] — deferred, rare on Pi, acceptable design limitation for now
- [x] [Review][Defer] `_cleanup()` cancels `_scheduler_task` without awaiting it — a job that swallows `CancelledError` internally may leave the task as a zombie on shutdown [`runtime.py:464-470`] — deferred, pre-existing pattern from prior loop cleanup

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Amelia / bmad-dev-story)

### Debug Log References

- Full default suite: `uv run pytest -q` → 338 passed, 3 skipped (platform-gated fork/privdrop), 3 deselected (`live`).
- Soak: `uv run pytest -m soak -q` → 2 passed (in-process `_seq == 2*turns`, `_bg` drains to 0), 1 skipped (real-fork, macOS).
- Contracts: `uv run lint-imports` → 2 kept, 0 broken ("core is LLM-free (AD-1)" KEPT — scheduler imports only stdlib datetime).
- Note: `test_provider_live_smoke` fails with a provider `RateLimitError` when `live` tests are force-included; it is `live`-marked and excluded from the default DoD suite — unrelated to this story.

### Completion Notes List

- **New `core/scheduler.py`** (LLM-free): `CostTier(REFLEX|TURN)`, three `Cadence` subclasses (`Interval`, `Idle`, `Daily`), a `Job` (name/cadence/cost_tier/run), and a `Scheduler` (injected clock, per-job `last_run`, `register`/`due`/`tick`). `tick` runs reflex jobs in-core under a per-job guard (one bad job logs + keeps ticking) and routes turn jobs to an injected `dispatch_turn` seam — **the scheduler never forks directly** (AD-14).
- **Cadence design (minimal, AD-14):** `Daily` is the cron-style minimum — once per calendar day at/after a UTC time `T`, NOT a 5-field grammar (limitation noted in-code). `Idle` fires once per idle stretch (re-arms on a fresh interaction, so a parked owner isn't pinged every tick). `Interval` is due immediately on first run (`last_run is None`); the soak parks via `scheduler_interval` (the loop sleeps before its first tick), which fully suppresses firing within the window.
- **AC2 — reflex tick is now a reflex job, unchanged:** `_reflex_tick` + `_maybe_push_mood_face` moved verbatim into `_run_reflex_job`; `_reflex_loop` deleted. Drift math, single-writer `apply_patch`, and "push the mood face only between turns on a token change" are byte-for-byte the same — they moved, they didn't change.
- **Task 3 decision — folded the checkpoint flush** into the scheduler as a second reflex job (`_run_checkpoint_job`); `_checkpoint_loop` deleted (no orphan sibling loop). The two singleton tasks (`_reflex_task`/`_checkpoint_task`) collapse into one `_scheduler_task` in its own slot (never `_bg`), cancelled in `_cleanup`.
- **AC3 — events bypass:** `run()`'s INBOUND_MSG/RESULT consumer is untouched; the scheduler is a sibling task created alongside it, not a gate in front of `core_inbox`. Proven by `test_inbound_message_bypasses_a_parked_scheduler` (turn completes with the scheduler parked an hour out).
- **5.2 seam left pointing at the arbiter:** no turn job is registered and `dispatch_turn` is unwired in the runtime, so nothing forks on a cadence in 5.1. The seam (cost-tier gate → arbiter, cooldown, daily credit/turn budget) is Story 5.2.
- **dev-loop-checklist applied:** per-job best-effort guard (never raises into the loop); tests assert real values + exercise rejection branches (duplicate name, non-positive `Interval`/`Idle`/`scheduler_interval`); turn-routing proven by a spy (turn job routes to dispatch, never runs in-core); no new file-write default path (no conftest change needed — the checkpoint job reuses the already-isolated `checkpoint_path`).

### File List

- `shelldon/core/scheduler.py` (new)
- `tests/test_scheduler.py` (new)
- `shelldon/core/runtime.py` (modified — scheduler wiring; `_reflex_loop`/`_checkpoint_loop` → jobs + `_scheduler_loop`; `scheduler_interval` param; `_cleanup`)
- `tests/test_reflexes.py` (modified — drive `_scheduler_loop`; `scheduler_interval` rejection test)
- `tests/test_state.py` (modified — checkpoint-flush test drives `_scheduler_loop`)
- `tests/test_end_to_end_turn.py` (modified — park scheduler in `build_harness`; AC3 bypass test)
- `tests/test_endurance_soak.py` (modified — park scheduler in both soak constructions; the background-emitter rule)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (status tracking)

## Change Log

| Date | Change |
|------|--------|
| 2026-06-18 | Story 5.1 implemented: core named-job scheduler (interval/daily/idle cadence + reflex/turn cost tier, AD-14). Reflex tick + checkpoint flush migrated to reflex-tier jobs; `_reflex_loop`/`_checkpoint_loop` deleted; one `_scheduler_task`. Events bypass confirmed (AC3). Background-emitter rule written into the soak (closes `soak-background-emitter-doc`). +18 scheduler tests; suite 338 pass / soak 2 pass; contracts KEPT. Turn-job dispatch left as the 5.2 seam. |
| 2026-06-18 | Code-review follow-ups (5 Patches) resolved: guarded the whole `tick()` in `_scheduler_loop` (a bad clock-read/due-computation can't kill the resident task); `Interval`/`Idle` guards now reject NaN (`not (x > 0)`); `Daily` rejects a tz-aware trigger time at construction; +4 tests (NaN/tz rejection, no-busy-loop on failure, the `dispatch_turn=None` production seam). 4 Defer items accepted (Idle prod-wiring → 5.4, `abc.ABC`, Daily clock-jump, cancel-without-await). Suite 342 pass / soak 2 pass; contracts KEPT. |
