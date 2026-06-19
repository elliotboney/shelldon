---
baseline_commit: 8a5610370620f6fae946b33c6a2fc7179829d266
---

# Story 5.2: Cost-tier gating and credit budget

Status: review

<!-- Second feature story of Epic 5. Builds directly on Story 5.1's `dispatch_turn` seam. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want background LLM activity capped,
so that the pet can't quietly burn through my API credits.

**Why now / what it unblocks:** Story 5.1 built the scheduler with `reflex` and `turn` cost tiers and left turn-job dispatch as an explicit seam — the scheduler routes a due `turn` job to an injected `dispatch_turn` hook that is **currently unwired** (logs + skips; nothing forks). 5.2 **fills that seam**: a due turn job becomes an **arbiter-gated, cooldown-gated, daily-budget-bounded** turn admission, so when 5.4 registers the first real proactive job (and Epic 6 the dream job) it is automatically capped. This is the spend guardrail behind the whole autonomous-mind line (AD-9/AD-14) — without it, a cadence-driven mind can wake itself up and spend uncontrollably. 5.0 made the turn lifecycle wedge-proof and 5.1 built the cadence engine; 5.2 makes self-driven turns **safe to spend**. Battery-aware backoff (5.3) and the proactive trigger itself (5.4) layer on top.

## Acceptance Criteria

### AC1 — Turn jobs are arbiter-gated, cooldown-gated, and daily-budget-bounded

**Given** turn jobs (reflection, dreaming, proactive)
**When** they become due
**Then** the arbiter runs **at most one turn at a time**, gated by a **cooldown** (a minimum interval between scheduler-initiated turns), and bounded by a **daily credit/turn budget**.

- A due `turn`-tier job is admitted **only if all three hold**: (1) the arbiter slot is free (`arbiter.is_idle` — AD-9 ≤1 in flight); (2) the cooldown since the last scheduler-initiated turn has elapsed; (3) the daily turn budget is not exhausted. On admission the turn goes through the **arbiter** exactly like an owner turn (AD-14: "the scheduler never forks directly").
- If the slot is busy or the cooldown has not elapsed → the turn job is **deferred** (skipped this tick; the cadence re-proposes it next due time — it is NOT folded into the owner's catch-up slot; only owner messages coalesce, AD-9).
- The budget + cooldown bookkeeping is **persistent** (survives a process restart) so a crash-loop or restart cannot reset the daily cap and overspend (AD-7). State is mutated through the single-writer `apply_patch` (AD-5).

### AC2 — Budget exhaustion skips non-essential turns; reflexes are unaffected

**Given** the daily budget is exhausted
**When** a non-essential (turn-tier) job is due
**Then** it is **skipped** (logged, not run) rather than forked; **reflex jobs (no LLM) continue unaffected** — mood drift, the checkpoint flush, and the between-turn mood-face push run on their normal cadence regardless of the turn budget.

- The gate lives **only** in the turn-tier dispatch path. Reflex jobs run via `job.run()` and never touch the budget/cooldown/arbiter gate, so an exhausted budget has zero effect on them.
- Budget resets on a **calendar-day rollover in the owner's local timezone** (decision 4 — `now.astimezone().date()`) — the first turn job due on a new local day finds the budget refreshed.

### Out of scope (explicit — later stories)

- **Battery-aware backoff** (PiSugar2 → stretch cadences / skip non-essential turns on battery) — **Story 5.3**. 5.2's budget is **credit/turn-count only**; no power-state read.
- **The proactive trigger itself** (greeting opportunity / mood-driven idle, CAP-4, no owner input) — **Story 5.4**. 5.2 builds and tests the **gated dispatch mechanism** with synthetic test turn jobs; it does **not** register a real proactive job in the composition root and does **not** define proactive trigger semantics.
- **Dreaming/reflection turn-job content** (AD-15, `capture_learning`, consolidation) — **Epic 6**. 5.2 gates such jobs; it does not implement them.
- **Real per-token / per-dollar credit accounting.** 5.2's budget is a **daily turn-COUNT** cap (with an optional per-job cost weight). True $-cost accounting (the broker holds provider/token detail) is a deferred future refinement — note the limitation, do not build it.
- **A plugin job-registration API** — Epic 7.

## Tasks / Subtasks

- [x] **Task 1 — The budget + cooldown policy (AC1, AC2)**
  - [x] Added `core/budget.py` (LLM-free, AD-1): `BudgetGate` is a pure policy over `(TurnBudget, now, cost)` — daily turn-count cap + minimum-interval cooldown + local-day rollover. Policy/driver split mirrors `core/reflexes.py`; injected clock, no sleeps.
  - [x] `evaluate` returns `Decision.ADMIT`/`DEFER`/`SKIP`; `admission_patch` returns the single-writer ledger patch (`budget.date`/`turns_used`/`last_turn_at`). Admission requires `used_today + cost <= daily_turn_budget`; a new local day resets `used_today` to 0.
  - [x] **Per-job cost weight (decision 3):** `Job` (in `scheduler.py`) gained `cost: int = 1` (+ `prompt`); the budget decrements by `job.cost`. Reflex tier ignores `cost`.
  - [x] `tests/test_budget.py`: ADMIT/DEFER/SKIP; SKIP-precedence-over-cooldown; `cost`>1 proportional + over-cap SKIP; local-day reset; `admission_patch` round-trip; non-positive/NaN config rejected; unparseable `last_turn_at` → no cooldown (never raises).
- [x] **Task 2 — Persist the budget ledger in personality state (AC1, AD-7/AD-5)**
  - [x] `core/state.py`: added `TurnBudget` substruct (`date`/`turns_used`/`last_turn_at`) to `PersonalityState` + the three dotted paths to `WRITABLE_PATHS`. Clean default on first run; a pre-5.2 checkpoint (no `budget` field) decodes to defaults (msgspec field default — the 3.1 legacy-tolerant restore holds).
  - [x] `tests/test_state.py`: ledger checkpoints + restores; pre-5.2 checkpoint loads to defaults; budget paths writable, off-set rejected; updated the closed-set assertion.
- [x] **Task 3 — Wire the `dispatch_turn` seam to the gate (AC1, AC2)**
  - [x] `core/runtime.py`: `Scheduler(now=..., dispatch_turn=self._dispatch_turn_job)`. `_dispatch_turn_job(job)`: skip if no prompt; defer if `not arbiter.is_idle`; else `BudgetGate.evaluate` → DEFER/SKIP (log + return) or ADMIT → record spend via `apply_patch`, `arbiter.submit(job.prompt)` (slot free → reserves), `_start_turn`. The admit sequence has no `await` so it is atomic w.r.t. the `run()` consumer (the arbiter's no-lock invariant holds with a second sibling task).
  - [x] Added `daily_turn_budget=12` + `turn_cooldown=1800.0` to `Core.__init__`; validation delegated to `BudgetGate` (like `Interval()`).
  - [x] A scheduler turn never enters the owner pending list (deferred when a turn is in flight); on completion the normal `_handle_result`/timeout → `arbiter.complete()` releases + folds owner catch-up. Release-safety holds (submit balanced by complete/reset, incl. the spawn-failure path).
- [x] **Task 4 — Integration tests: the gate end-to-end (AC1, AC2)**
  - [x] `tests/test_turn_dispatch.py`: admit-and-start; defer-in-cooldown; skip-when-exhausted; defer-when-in-flight (NOT coalesced into the owner slot); cost-weight spends multiple; promptless skipped (no wedge); spawn-failure counts the spend AND releases the slot; reflex job unaffected by an exhausted budget; **budget survives a restart** (cap loaded from disk, not re-granted).
- [x] **Task 5 — Soak + full-suite + contracts**
  - [x] Soak unaffected (no turn job registered; scheduler parked — 5.1): `_seq == 2*turns` and `_bg` drains still hold (2 passed). No new resident emitter. The budget ledger reuses the already-isolated `checkpoint_path` → **no conftest change** (no new write-default path).
  - [x] Full `pytest` green (367 passed) incl. the soak (2 passed); both import-linter contracts **KEPT** (`core/budget.py` in `core/`, LLM-free); `dev-loop-checklist.md` applied.

### Review Findings

- [x] [Review][Patch] Future `last_turn_at` permanently DEFERs with no recovery or warning [`shelldon/core/budget.py:49`] — **FIXED**: `_seconds_since` now treats a future stamp (negative elapsed) as no active cooldown + warns; +`test_future_last_turn_at_recovers_instead_of_deferring_forever`.
- [x] [Review][Patch] REFLEX job with `run=None` logs TypeError every scheduler tick [`shelldon/core/scheduler.py:181`] — **FIXED**: `Job.__init__` rejects a REFLEX job with no `run` (fail fast); +`test_reflex_job_requires_a_run_callable`.
- [x] [Review][Patch] `Job.cost=0` bypasses budget entirely; `cost<0` decrements spend [`shelldon/core/scheduler.py:126`] — **FIXED**: `Job.__init__` rejects `cost < 1`; +`test_job_rejects_nonpositive_cost`.
- [x] [Review][Patch] `arbiter.submit` return value not None-guarded on ADMIT path [`shelldon/core/runtime.py:435`] — **FIXED**: reordered to reserve-then-spend; `submit` return is None-guarded (logs + returns without spending). The None branch is provably unreachable while `is_idle` holds in the await-free section, so it's defensive (no test for the impossible path).
- [x] [Review][Defer] `Daily` cadence uses UTC-day; budget uses local-day — silently misaligned [`shelldon/core/scheduler.py:104`] — deferred, pre-existing (introduced Story 5.1)
- [x] [Review][Defer] DEFER vs SKIP paths not distinguished by log assertion in integration tests [`tests/test_turn_dispatch.py`] — deferred, minor test expressiveness gap

## Dev Notes

**This story fills the 5.1 turn-dispatch seam with a spend gate — it does not invent proactive behavior or dreaming.** The hard part is layering the cooldown + daily budget onto the existing arbiter/turn machinery **without** (a) corrupting the owner catch-up coalescing, (b) letting a restart reset the daily cap, or (c) touching the reflex path. Read 5.1's scheduler seam, the arbiter, and the state substrate before writing code.

### The seam being filled (read these first)

- [Source: `shelldon/core/scheduler.py`] `Scheduler.tick` routes a due `CostTier.TURN` job to `self._dispatch_turn(job)`; `_dispatch(job)` calls the injected `dispatch_turn` hook, or — when unwired — logs `"turn job %r due — dispatch is Story 5.2 (arbiter-gated); not forking"` and returns. **5.2 wires `dispatch_turn`.** The per-job guard in `tick` already catches a raising dispatch (logs + keeps ticking), so `_dispatch_turn_job` does not need its own outer try/except for the "keep ticking" guarantee — but it MUST leave the arbiter/fence consistent if it fails mid-admission.
- [Source: `shelldon/core/runtime.py`] `__init__` constructs `self.scheduler = Scheduler(now=lambda: datetime.now(UTC))` **with no `dispatch_turn`** and the comment "Turn-tier dispatch (cooldown + credit budget + the arbiter gate) is Story 5.2: the scheduler's `dispatch_turn` seam is left unwired here." **Change that line** to pass `dispatch_turn=self._dispatch_turn_job` and add the method. `_start_turn(prompt)` opens the fence, pushes the thinking face, spawns the worker, arms the timeout — reuse it verbatim; do not duplicate turn-start logic.
- [Source: `shelldon/core/arbiter.py`] `submit(text) -> str | None` reserves the slot and returns the prompt when idle, else folds into the pending catch-up list. `complete()`/`reset()` release. **Reuse `submit` for an idle-slot scheduler turn** (you already checked `is_idle`, so it returns the prompt and reserves). Do **not** add a second admission method unless a genuine need appears — the existing release-safety invariant (5.0 AC4: every admission reaches `complete` or `reset`) must continue to hold for scheduler-initiated turns too (the completion path is the same `_handle_result`/timeout, so it does).
- [Source: `shelldon/core/state.py`] `PersonalityState` (msgspec.Struct, mutable RAM), `WRITABLE_PATHS` (closed dotted-path set — a key outside it is a rejected patch), `apply_patch` (single writer, marks dirty), atomic checkpoint/restore with corrupt-state fallback to defaults. **Add the budget ledger here** following the `Mood` substruct precedent; the checkpoint loop (now a reflex job, 5.1) flushes it.

### Budget / cooldown design (keep it minimal — AD-9, not an accounting system)

- **Unit = daily turn COUNT** (default **12/day**), not dollars/tokens. The owner's concern is "don't quietly burn credits"; a per-day cap on scheduler-initiated turns satisfies CAP-8 minimally. A per-job `cost` weight (default 1, decision 3) makes a future dream turn count heavier than a ping — build the mechanism now, keep it simple. True $-accounting is deferred (the broker, not core, knows token cost) — note the limitation, do not build it.
- **Two independent gates:** the **cooldown** (default **30 min**) prevents a stampede (min seconds between scheduler turns — proactive pings shouldn't be frequent); the **budget** caps total daily spend. Both must pass to admit. They are orthogonal: a fresh day with the cooldown still running → DEFER; mid-day with budget exhausted → SKIP.
- **Persist the ledger** (`date` + `turns_used`, and the cooldown stamp `last_turn_at`) in `PersonalityState` so a restart cannot reset the cap (this is the whole point — a crash-loop must not re-grant the daily budget every boot). Reset `turns_used` to 0 when the **local** calendar date of `now` (via `now.astimezone().date()`, decision 4) differs from the stored `date`. Mirror the reflex policy's defensive timestamp parsing for `last_turn_at` (an unparseable/tz-naive value → treat as "no cooldown active", never raise — see `reflexes._idle_seconds`).
- **Inject the clock** so day-rollover and cooldown are unit-testable without sleeping (the reflex/scheduler precedent). The budget policy is pure: `(state, now, config) → decision + patch`. The runtime applies the patch via `apply_patch` and does the I/O (spawn).

### Scheduler turns vs owner coalescing (the trap)

A scheduler turn job must be admitted **only when `arbiter.is_idle`**. If a turn is in flight, **defer** the job (it re-proposes next cadence) — do **not** push it through `arbiter.submit`, which would coalesce the proactive prompt into the owner's pending catch-up slot and corrupt the owner's next turn. Only **owner** messages (the `run()` INBOUND_MSG path) coalesce; scheduler turns are fire-when-idle. Conversely, when a proactive turn IS in flight and an owner message arrives, the existing INBOUND_MSG path coalesces it normally and `arbiter.complete()` folds it into a catch-up turn after the proactive turn ends — that already works; just don't break it.

### Architecture invariants (binding)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-9** (lines 112-115): the arbiter is the single gate for turns — **≤1 in flight**, coalescing, degrade-to-reflex. "**All turn-jobs … carry a cost** and are additionally gated by a **daily credit/turn BUDGET** and battery-aware backoff … the scheduler proposes cadence-driven turn-jobs, **the arbiter is the single gate that admits or drops them**." 5.2 implements the cooldown + daily budget half (battery is 5.3).
- **AD-14** (lines 137-140): "Scheduler-proposed turn jobs go through the **arbiter** (AD-9) — same ≤1-worker bound, coalescing, and credit/battery gate as every other turn; **the scheduler never forks directly**." 5.2 honors this — dispatch admits through the arbiter + `_start_turn`, never a direct `spawn_turn`.
- **AD-5**: the budget ledger is mutated ONLY through the single-writer `apply_patch`.
- **AD-7**: the ledger persists via the periodic checkpoint (so a restart can't reset the cap).
- **AD-1**: `core/budget.py` is LLM-free and imports no provider/worker code (import-linter KEPT).
- **AD-12**: scheduler-initiated turns carry a `turn_id` and are fenced exactly like owner turns (reusing `_start_turn` gives this for free).

### Testing standards

- `pytest`; **deterministic clock injection, never `asyncio.sleep` anchors** for cooldown/day-rollover (Epic 2 retro #1). Use the shared `await_true` for integration-level polling.
- New `tests/test_budget.py` for the pure policy; extend `tests/test_state.py` for the ledger persistence + back-compat decode; integration tests alongside the scheduler/runtime suites for the gated dispatch.
- **Apply `dev-loop-checklist.md`** before review: best-effort guard on the dispatch hook; tests assert real values (`turns_used == cap`, not truthiness) AND exercise the DEFER/SKIP/reset branches; reject non-positive/NaN budget+cooldown config (the 5.1 cadence-guard precedent); no false-positive masking; conftest isolation is unchanged (budget reuses the isolated `checkpoint_path` — call this out).
- Run the **soak** (`-m soak`) locally — it must stay green and unchanged (no turn job registered, scheduler parked). Budget writes only happen on turn-job admission, of which the soak has none, so `_seq`/`_bg`/heap are unaffected — confirm.

### Project Structure Notes

- New: `shelldon/core/budget.py`, `tests/test_budget.py`. Modified: `shelldon/core/scheduler.py` (`Job` gains optional `cost: int = 1` — decision 3), `shelldon/core/state.py` (budget ledger substruct + `WRITABLE_PATHS`), `shelldon/core/runtime.py` (wire `dispatch_turn`, add `daily_turn_budget`=12/`turn_cooldown`=1800, `_dispatch_turn_job`), `tests/test_state.py` (ledger persistence + back-compat), `tests/test_scheduler.py` (cost-weight round-trip), and the runtime integration tests. The arbiter is **reused unchanged** (reuse `submit`/`complete`); add a method there only if a real need surfaces.
- LLM-free core (AD-1) must stay **KEPT**. No new real-`$HOME` write path (the ledger rides the existing `checkpoint_path`).

### Previous-story intelligence (Story 5.1 — done; Story 5.0 — done)

- **5.1 built exactly this seam and left it labeled.** `Scheduler(now=...)` has a `dispatch_turn` ctor param (default `None` → log+skip). `tick` already (a) marks `last_run` before running so a failed dispatch waits its period (no busy-loop), and (b) guards each job (a raising dispatch logs + keeps ticking). 5.2 just supplies the hook. The `CostTier.TURN` tier, the `Job` model, and the routing are done + tested — **do not re-invent them**.
- **5.1 patterns to carry:** pure policy in its own module + injected clock + deterministic tests (no sleeps); reject non-positive AND NaN numeric config via `not (x > 0)`; reject tz-aware time inputs at construction; park any new resident emitter in the soak (none added here); write rejection/branch tests, not just the happy path; guarded best-effort everywhere.
- **5.0 made the turn lifecycle wedge-proof** (coherent timeout chain W<R<T, ≤1 slot always releases, reap awaited before the arbiter frees). Scheduler-initiated turns ride this same hardened path automatically by reusing `_start_turn` + the `_handle_result`/timeout completion — **do not add a separate lifecycle for proactive turns.**
- **5.1 review lesson (apply preemptively):** guard the scaffolding, not just the inner call; pin "no busy-loop on failure" and the "unwired hook" production seam with explicit tests; validate numeric/time inputs at construction (fail fast) rather than failing silently every tick.

### Resolved decisions (owner, 2026-06-18 — binding)

1. **`daily_turn_budget` default = 12** scheduler-initiated turns/day. Injectable.
2. **`turn_cooldown` default = 1800s (30 min)** minimum gap between scheduler turns. Injectable.
3. **Per-job cost weight IS in scope.** Extend the `Job` model (`shelldon/core/scheduler.py`) with an optional `cost` weight (default `1`); the budget decrements `turns_used` by the job's `cost`, and admission requires `turns_used + cost <= daily_turn_budget`. This lets Epic 6's dream turn declare a **heavier** cost (e.g. `cost=3`) so one dream counts as several pings against the cap. Build the **mechanism** now (default 1, fully tested); the dream job that sets a heavier cost lands in Epic 6.
4. **Day-rollover boundary = the owner's LOCAL timezone**, not UTC. The injected clock still returns tz-aware UTC (`datetime.now(UTC)`); the budget policy derives the local calendar date via `now.astimezone()` (no tz arg → system local) so "daily" means the owner's day. Cooldown is elapsed-seconds (tz-agnostic). Tests inject a fixed aware `now` and assert the rollover at the local-date boundary.

## References

- [Source: `_bmad-output/planning-artifacts/epics.md`#Story-5.2 (lines 573-587)] — the two ACs verbatim.
- [Source: ARCHITECTURE-SPINE.md] AD-9 (112-115), AD-14 (137-140), AD-5, AD-7, AD-1, AD-12.
- [Source: `shelldon/core/scheduler.py`] the `dispatch_turn` seam (`Scheduler.__init__`, `tick`, `_dispatch`) this story fills.
- [Source: `shelldon/core/runtime.py`] `Scheduler(...)` construction (the unwired seam), `_start_turn`/`_handle_result`/`_timeout_watch` (the turn lifecycle reused), `__init__` interval-param + positive-check precedent.
- [Source: `shelldon/core/arbiter.py`] `submit`/`complete`/`reset` + the 5.0 release-safety invariant.
- [Source: `shelldon/core/state.py`] `PersonalityState`/`Mood`/`WRITABLE_PATHS`/`apply_patch`/checkpoint — the ledger's home and single-writer path.
- [Source: `shelldon/core/reflexes.py`] `_idle_seconds` defensive timestamp parsing — mirror for `last_turn_at`.
- [Source: `_bmad-output/implementation-artifacts/5-1-core-scheduler-with-named-multi-cadence-jobs.md`] the seam, the cost-tier model, and the review lessons to apply preemptively.
- [Source: `_bmad-output/implementation-artifacts/dev-loop-checklist.md`] the pre-review self-checklist.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Amelia / bmad-dev-story)

### Debug Log References

- Full default suite: `uv run pytest -q` → 367 passed, 3 skipped (platform-gated fork/privdrop), 3 deselected (`live`).
- Soak: `uv run pytest -m soak -q` → 2 passed (`_seq == 2*turns`, `_bg` drains), 1 skipped (real-fork, macOS). Unchanged by 5.2 — no turn job registered, scheduler parked.
- Contracts: `uv run lint-imports` → 2 kept, 0 broken ("core is LLM-free (AD-1)" KEPT — `core/budget.py` imports only stdlib datetime + the state struct).

### Completion Notes List

- **New `core/budget.py`** (LLM-free): `Decision(ADMIT|DEFER|SKIP)` + `BudgetGate` (pure policy, injected clock). `evaluate` = SKIP if `used_today + cost > daily_turn_budget` (reported even inside the cooldown — it won't run today regardless), else DEFER if inside the cooldown, else ADMIT. `admission_patch` is the single-writer ledger mutation. Two owner decisions baked in: **local-day** rollover (`now.astimezone().date()`) and a per-job **cost** weight.
- **Persisted ledger (`TurnBudget` in `core/state.py`)** — `date`/`turns_used`/`last_turn_at`, added to `WRITABLE_PATHS`. Persisting is the whole point: a crash-loop/restart can't reset the daily cap (proven by `test_budget_survives_a_restart`). A pre-5.2 checkpoint decodes to defaults (msgspec field default).
- **`Job` gained `cost: int = 1` + `prompt`** (decision 3) — a future dream turn declares `cost=3`; reflex jobs ignore both.
- **`_dispatch_turn_job` fills the 5.1 seam:** promptless → skip (no wedge); slot busy → defer (NEVER coalesced into the owner catch-up — only owner messages coalesce); else budget `evaluate` → DEFER/SKIP or ADMIT (record spend, `arbiter.submit` reserves, `_start_turn`). Spend is recorded BEFORE the spawn (conservative — a failed fork still counts), and `_start_turn`'s failure path `reset()`s the arbiter, so release-safety (5.0) holds for scheduler turns too.
- **Concurrency:** the scheduler runs as a sibling task to the `run()` consumer, but the admit critical section (is_idle check → apply_patch → submit) has **no `await`**, so it's atomic w.r.t. the consumer — the arbiter keeps its no-lock single-critical-section invariant (5.0/AD-9). Noted in-code.
- **Scope honored:** no battery read (5.3), no proactive trigger / no real turn job registered (5.4), no dream content (Epic 6), no $-token accounting (turn-count only). The gate is live + tested; 5.4 just registers a proactive `turn` job and it's automatically capped.
- **dev-loop-checklist applied:** defensive `last_turn_at` parse (never raises); config rejects non-positive/NaN; tests assert real values + every branch (ADMIT/DEFER/SKIP, cost-over-cap, rollover, promptless, spawn-failure, restart, reflex-unaffected); budget reuses the isolated `checkpoint_path` → no conftest change.

### File List

- `shelldon/core/budget.py` (new)
- `tests/test_budget.py` (new)
- `tests/test_turn_dispatch.py` (new)
- `shelldon/core/state.py` (modified — `TurnBudget` substruct + `WRITABLE_PATHS`)
- `shelldon/core/scheduler.py` (modified — `Job` gains `cost` + `prompt`)
- `shelldon/core/runtime.py` (modified — `BudgetGate` wiring, `daily_turn_budget`/`turn_cooldown`, `_dispatch_turn_job`)
- `tests/test_state.py` (modified — ledger persistence + back-compat + closed-set assertion)
- `tests/test_scheduler.py` (modified — `Job` cost/prompt round-trip)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (status tracking)

## Change Log

| Date | Change |
|------|--------|
| 2026-06-18 | Story 5.2 implemented: turn-job spend gate (AD-9/AD-14). New `core/budget.py` (`BudgetGate`: daily turn-count cap=12 + 30-min cooldown + local-day rollover, per-job cost weight); persisted `TurnBudget` ledger in state (restart can't reset the cap); `Job` gains `cost`+`prompt`; `_dispatch_turn_job` wires the 5.1 seam — arbiter-gated admit/defer/skip, never coalesced into the owner slot, never forks directly. Reflexes unaffected by exhaustion. +25 tests (test_budget, test_turn_dispatch, +state/scheduler); suite 367 pass / soak 2 pass; contracts KEPT. Proactive trigger=5.4, battery=5.3, dream content=Epic 6, $-accounting deferred. |
| 2026-06-18 | Code-review follow-ups (4 Patches) resolved: future `last_turn_at` now recovers instead of deferring forever; `Job` rejects a REFLEX with no `run` and `cost < 1` (fail fast); ADMIT path reordered to reserve-then-spend with a defensive None-guard on `submit`. +3 tests. 2 Defer items accepted (Daily UTC-day vs budget local-day; DEFER/SKIP log assertion). Suite 370 pass / soak 2 pass; contracts KEPT. |
