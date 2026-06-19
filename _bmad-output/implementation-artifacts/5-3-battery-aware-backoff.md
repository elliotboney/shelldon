---
baseline_commit: 8a5610370620f6fae946b33c6a2fc7179829d266
---

# Story 5.3: Battery-aware backoff

Status: done

<!-- Third feature story of Epic 5. Builds on Story 5.1's scheduler (cadence + cost tiers) and Story 5.2's turn-dispatch gate. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to ease off when it's on battery,
so that autonomy doesn't drain the PiSugar2 pack.

**Why now / what it unblocks:** Story 5.1 built the scheduler (named jobs, per-job cadence + cost tier) and Story 5.2 made scheduler-initiated turns *credit-safe* (arbiter + cooldown + daily budget). 5.3 adds the **second autonomy guardrail AD-14 requires: power-awareness.** A mind that wakes itself up must also back off when running on the PiSugar2 pack — stretch its cadences (fewer wakeups) and skip non-essential LLM turns on battery / low charge, then return to livelier cadences when plugged in. This is the **battery half** of AD-9's "daily credit/turn budget **and** battery-aware backoff." It closes the autonomy safety story (5.0 wedge-proof → 5.1 cadence engine → 5.2 credit gate → **5.3 battery gate**) so that when 5.4 registers the first real proactive job (and Epic 6 the dream job) it is automatically both credit- AND battery-bounded.

**The honest seam (read this first).** The architecture says the scheduler reads **PiSugar2** power state (AD-14), but PiSugar2 is a **plugin-host plugin** (AD-8) and **plugin-host is Epic 7 — not built yet.** So 5.3 builds the **power-state seam + the backoff policy**, fed by an **injected power reader** that defaults to a *plugged-in* stub. The real PiSugar2 read (its local HTTP/socket API, surfaced to core via a plugin-host power envelope) lands in Epic 7 and swaps the stub — **zero policy change.** This mirrors exactly how 5.2 built and tested the gated-dispatch *mechanism* with synthetic turn jobs without registering a real proactive job. The backoff is fully live and tested against a controllable reader; only the hardware read is deferred.

## Acceptance Criteria

### AC1 — On battery / low charge: stretch cadences and skip non-essential LLM turns

**Given** the scheduler reading PiSugar2 power state
**When** the pet is on battery or at low charge
**Then** it **stretches job cadences** (fewer wakeups) and **skips non-essential LLM turn jobs**.

- Power state resolves to one of **three backoff levels** (owner decision 1): **LIVELY** (plugged in / charging), **EASED** (on battery, charge ≥ low-charge threshold), **LOW** (on battery, charge < threshold). The level is computed **purely** from the power reading each tick — no persistence (power is read live, nothing to checkpoint).
- **Cadence stretch applies to ALL `Interval`/`Idle` job cadences** (reflex **and** turn — owner decision 2), by a per-level scale factor (`1.0` LIVELY, `eased_scale` EASED, `low_scale` LOW). `Daily` cadence is **exempt** (it is already once-per-day; a nightly job's *time-of-day* trigger is not stretched — but a `Daily` **turn** job is still subject to the turn-skip below).
- **Turn-skip (turn-tier jobs only):** under **EASED**, a **non-essential** turn job is skipped (logged, not dispatched); an **essential** turn job still runs. Under **LOW**, **all** turn jobs are skipped (including essential). Under **LIVELY**, nothing is skipped. "Essential" is a new per-`Job` flag (`essential: bool = False`) — mechanism for a future critical job, exactly as 5.2's `cost` weight was built ahead of the dream job. **Reflex jobs are never skipped by backoff** — only their cadence stretches (they are cheap, no LLM, and carry aliveness).
- The battery gate is an **outer gate over the 5.2 budget gate**: the scheduler decides battery-skip *before* calling the `dispatch_turn` hook, so a battery-skipped turn never even reaches the cooldown/budget check. 5.2's `_dispatch_turn_job` is **unchanged**.

### AC2 — Plugged in / ample power: livelier cadences

**Given** the pet is plugged in / charging
**When** power is ample
**Then** it returns to **livelier (normal) cadences** — LIVELY level: scale `1.0`, no turn-skip. Behavior is **identical to pre-5.3** (regression: the default injected reader is the plugged-in stub, so an un-wired deployment behaves exactly as 5.2 did).

- "Plugged in" ⇒ LIVELY **regardless of charge** (a low battery that is **charging** is recovering, not backing off — owner decision). Only `on_battery` triggers EASED/LOW.

### AC3 — Backoff is demonstrable under simulated power state (CAP-10)

**Given** simulated battery / low-charge state in a test
**When** the scheduler evaluates jobs
**Then** the backoff is **demonstrable**: cadences stretched (a job due at period `T` under LIVELY is not due until `T × scale` under EASED/LOW) **and** non-essential turns skipped — driven by a **controllable injected power reader + injected clock, no `asyncio.sleep` anchors** (the 5.1/5.2 deterministic-test rule). This is the CAP-10 battery-aware-mind success proof.

### Out of scope (explicit — later stories / runtime)

- **The real PiSugar2 read** (its local HTTP/socket power API) — that is a **plugin-host PiSugar2 plugin (Epic 7 / AD-8)** that surfaces power to core. 5.3 injects a **stub reader** (default plugged-in) and notes the Epic 7 cache-update seam; it does **not** open a socket, add a dependency, or define the plugin manifest.
- **The bus envelope/contract for plugin-host → core power updates** (a plugin-emitted `event` kind, AD-11/AD-8) — Epic 7. 5.3 reads a cached/injected `PowerState` value in-core; it does **not** add a wire contract.
- **Registering any real turn job** (proactive = 5.4, dream = Epic 6). 5.3 tests the gate with **synthetic** turn jobs (essential + non-essential). No turn job is registered in the composition root.
- **Charging-rate / time-to-full / battery-health heuristics**, hysteresis/debounce on the level transition, and per-job custom battery policy beyond the `essential` flag — runtime refinements, not this story.
- **Persisting power state** — power is read live each tick; there is **nothing to checkpoint** (contrast 5.2's ledger). No `state.py` change.

## Tasks / Subtasks

- [x] **Task 1 — The power model + backoff policy (`core/power.py`, new) (AC1, AC2)**
  - [x] `PowerState` (a small msgspec.Struct or frozen struct, RAM-only — **not** a `contracts/` bus type, like `PersonalityState`): `on_battery: bool`, `charge: float | None` (0.0–1.0, `None` = unknown). Constructed by the reader; the policy only reads it.
  - [x] `BackoffLevel` enum: `LIVELY` / `EASED` / `LOW`.
  - [x] `BackoffPolicy` (pure policy, the `reflexes.py`/`budget.py` precedent — config only, no I/O, no clock needed): `__init__(*, eased_scale=3.0, low_scale=6.0, low_charge_threshold=0.20)`. **Validate at construction** (5.1/5.2 fail-fast precedent): `eased_scale >= 1.0` and `low_scale >= 1.0` via `not (x >= 1.0)` (rejects NaN + <1, which would *speed up* on battery); `0 < low_charge_threshold <= 1` via the same `not (...)` idiom.
  - [x] `level(power) -> BackoffLevel`: **plugged (`not on_battery`) ⇒ LIVELY regardless of charge**; on battery + `charge is not None and charge < low_charge_threshold` ⇒ **LOW**; else (on battery, charge OK **or unknown**) ⇒ **EASED**. (Unknown charge on battery is **EASED, never LOW** — never escalate to the deepest backoff on a missing reading.)
  - [x] `cadence_scale(level) -> float`: `1.0` / `eased_scale` / `low_scale`.
  - [x] `skips(level, *, essential) -> bool`: LIVELY → `False`; LOW → `True`; EASED → `not essential`. (Caller invokes this **only for turn-tier jobs** — reflex jobs are never skipped.)
  - [x] LLM-free (AD-1): imports only stdlib `enum` (+ `msgspec` for the struct). No provider/worker import. Import-linter stays **KEPT**.
- [x] **Task 2 — Cadence stretch on the scheduler (`core/scheduler.py`) (AC1)**
  - [x] Add an optional `scale: float = 1.0` to `Cadence.is_due(...)` and its three subclasses: `Interval` compares `>= self.period_s * scale`; `Idle` compares `< self.period_s * scale` (both stretch); `Daily` **ignores** `scale` (once/day). Default `1.0` keeps every existing caller/test behavior-identical.
  - [x] `Scheduler.due(now, last_interaction=None, scale=1.0)` threads `scale` into each `is_due` call.
  - [x] `Job` gains `essential: bool = False` (keyword-only, alongside `cost`/`prompt`). Reflex jobs ignore it (never skipped). No other `Job` change.
- [x] **Task 3 — Wire power + backoff into the scheduler tick (`core/scheduler.py`, `core/runtime.py`) (AC1, AC2)**
  - [x] `Scheduler.__init__` gains injected `power: Callable[[], PowerState]` and `backoff: BackoffPolicy`. The power reader is a **synchronous, non-blocking** callable returning the latest cached `PowerState` (it must **not** block the tick on I/O — the real Epic 7 plugin pushes updates; the scheduler reads a cached value).
  - [x] `tick()`: read `power()` → `level` → `scale = backoff.cadence_scale(level)`; compute `due(now, last_interaction, scale=scale)`; for each due job, mark `last_run` (before running, as today — no busy-loop), then: reflex tier → `await job.run()` (stretched but never skipped); turn tier → **if `backoff.skips(level, essential=job.essential)`: log + skip** (do NOT dispatch); else `await self._dispatch(job)`. The per-job guard already wraps this.
  - [x] `core/runtime.py`: construct `BackoffPolicy(eased_scale=..., low_scale=..., low_charge_threshold=...)` from new module-level defaults (`DEFAULT_EASED_SCALE=3.0`, `DEFAULT_LOW_SCALE=6.0`, `DEFAULT_LOW_CHARGE_THRESHOLD=0.20`); add the three as injectable `Core.__init__` params (validation delegated to `BackoffPolicy`, like `Interval()`/`BudgetGate`). Inject `power=` (default the plugged-in stub `lambda: PowerState(on_battery=False, charge=None)`) and `backoff=` into the `Scheduler(...)` construction. Add an injectable `power=None` param to `Core.__init__` → default stub, so tests pass a controllable reader.
  - [x] **Leave `_dispatch_turn_job` (5.2) untouched** — battery is an outer scheduler gate; the budget gate runs only on jobs that survive the battery skip.
- [x] **Task 4 — Tests: policy + scheduler + integration (AC1, AC2, AC3)**
  - [x] `tests/test_power.py` (new): `level` truth table (plugged→LIVELY incl. plugged+low; battery+ok→EASED; battery+low→LOW; battery+unknown-charge→EASED); `cadence_scale` per level; `skips` matrix (LIVELY never; EASED skips non-essential only / runs essential; LOW skips all incl essential); config rejects `scale < 1`, NaN scale, threshold `≤ 0` / `> 1` / NaN.
  - [x] `tests/test_scheduler.py` (extend): scaled due-ness (`Interval`/`Idle` stretch by scale; `Daily` ignores scale); `Job.essential` round-trip; **LIVELY = unchanged behavior** (regression); tick under EASED skips a non-essential turn job but runs an essential turn job AND the reflex job; tick under LOW skips ALL turn jobs (incl essential) while reflex jobs still run; assert `last_run` advances on a skip (re-proposed next stretched cadence, no busy-retry).
  - [x] `tests/test_battery_backoff.py` (new, integration — AC3 / CAP-10): with a controllable power reader + injected clock, show a turn job due at `T` under LIVELY is **not** due until `T × eased_scale` under EASED, and a non-essential turn is **skipped** on battery; flip the reader to plugged → the turn dispatches normally (livelier). No `asyncio.sleep` anchors; use the shared `await_true` for any polling.
- [x] **Task 5 — Soak + full-suite + contracts**
  - [x] Soak (`-m soak`) stays green/unchanged: the default power reader is the **plugged-in stub** (LIVELY, scale 1.0, no skip), so an un-instrumented scheduler behaves exactly as 5.1/5.2. No turn job registered; scheduler still parkable via `scheduler_interval`. No new resident emitter. **No conftest change** (no new real-`$HOME` write path — power is read-only, in-RAM).
  - [x] Full `pytest` green incl. soak; both import-linter contracts **KEPT** (`core/power.py` in `core/`, LLM-free); apply `dev-loop-checklist.md`.

## Dev Notes

**This story adds power-awareness to the scheduler — it does not read real hardware and does not invent new turn jobs.** The hard part is layering cadence-stretch + turn-skip onto the 5.1 scheduler **without** (a) touching the reflex execution path's behavior (only its cadence stretches), (b) disturbing 5.2's `_dispatch_turn_job` gate, or (c) introducing a blocking I/O read inside the tick. Read 5.1's scheduler, 5.2's dispatch seam, and `reflexes.py`/`budget.py` (the policy/driver shape) before writing code.

### The seam being filled (read these first)

- [Source: `shelldon/core/scheduler.py`] `Cadence.is_due(now, last_run, last_interaction)` + the `Interval`/`Idle`/`Daily` subclasses; `Scheduler.due()` and `Scheduler.tick()`; the `Job` model (already carries `cost`/`prompt` from 5.2). **You add the `scale` param to `is_due`, thread it through `due`/`tick`, add `Job.essential`, and add the power read + skip to `tick`.** The per-job guard in `tick` and the mark-`last_run`-before-running rule (no busy-loop) are already there — keep them.
- [Source: `shelldon/core/runtime.py`] `__init__` constructs `self.scheduler = Scheduler(now=lambda: datetime.now(UTC), dispatch_turn=self._dispatch_turn_job)` and registers the `reflex` + `checkpoint` reflex jobs. **Change the `Scheduler(...)` line** to also pass `power=` and `backoff=`. Add the three backoff defaults + the `power=` injectable param. **Do not touch `_dispatch_turn_job`** — the battery skip is decided in the scheduler before dispatch is ever called.
- [Source: `shelldon/core/budget.py`] the policy/driver split + fail-fast `not (x > 0)` config validation + the `Decision` enum shape — mirror it for `BackoffPolicy` / `BackoffLevel`. `BackoffPolicy` is even simpler: it needs **no clock** (no time math; power is instantaneous).
- [Source: `shelldon/core/reflexes.py`] the pure-policy-in-its-own-module + injected-driver precedent (and `_idle_seconds` defensive parsing, if you read any timestamp — you won't here; power has no timestamp).
- [Source: `shelldon/core/state.py`] **read, do not modify.** Confirms there is no persistence need: power state is live, not checkpointed. (Contrast 5.2, which added the `TurnBudget` ledger here.)

### Backoff design (keep it minimal — AD-14, not a power-management subsystem)

- **Three levels, computed each tick from one reading** (owner decision 1): LIVELY (plugged), EASED (battery, charge OK/unknown), LOW (battery, charge < `low_charge_threshold`). Plugged ⇒ LIVELY **regardless of charge** (charging = recovering, not backing off). Unknown charge on battery ⇒ EASED, never LOW (don't deepen on a missing reading).
- **Cadence stretch is multiplicative on the period**, applied to **all `Interval`/`Idle` cadences** (owner decision 2 — reflex + turn). `Daily` is exempt (once/day; a `Daily` *turn* job is instead handled by the turn-skip). A skipped/stretched job's `last_run` still advances on the tick it was due, so the next due time is `now + period×scale` — no busy-retry.
- **Turn-skip is the LLM-spend lever** and applies **only to turn-tier jobs**: EASED skips non-essential, LOW skips all. Reflex jobs (no LLM, cheap CPU, carry aliveness) are **never** skipped — only their cadence stretches. The `essential` flag is the future-proofing mechanism (today every turn job defaults non-essential, so EASED and LOW both skip all current turn jobs; they differ only in stretch factor and the `essential` carve-out a future critical job will use — exactly the "build the mechanism now" call from 5.2's `cost`).
- **The power reader is a non-blocking cached read.** AD-14 says the scheduler *reads* PiSugar2 power; the actual read (Epic 7's plugin) pushes updates to core, which caches a `PowerState` the scheduler samples synchronously each tick. For 5.3 the reader is an **injected stub** (`lambda: PowerState(on_battery=False, charge=None)` → LIVELY). **Do not** put a blocking socket/HTTP call inside `tick`. Note the Epic 7 cache-update seam in a comment; do not build it.

### Gate layering (the trap)

The battery gate and the 5.2 budget gate are **two distinct gates in series**, in this order on a due turn job: **(1) battery skip (scheduler, `tick`)** → if not skipped → **(2) `_dispatch_turn_job` (cooldown + daily budget, runtime)** → if admitted → `_start_turn`. Keep them separate: battery logic stays in the scheduler (it owns cadence + power, AD-14); credit logic stays in `_dispatch_turn_job` (unchanged). Do **not** fold battery into the budget gate or vice-versa — they answer different questions (power vs credit) and 5.4/Epic 6 jobs must clear both.

### Architecture invariants (binding)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-14** (lines 137-140): "The scheduler is **BATTERY-AWARE**: it reads **PiSugar2** power state and **stretches cadences / skips non-essential LLM turns** on battery or low charge, and runs **livelier when plugged in**." 5.3 implements this against an injected reader (real read = Epic 7 plugin).
- **AD-9** (lines 112-115): "All turn-jobs … are additionally gated by a daily credit/turn BUDGET **and battery-aware backoff** — the arbiter reads PiSugar2 power state (**via the scheduler, AD-14**) and skips or defers non-essential LLM turns on battery / low charge." 5.3 supplies the battery half; the scheduler is the reader, the skip is decided before dispatch.
- **AD-8** (line 110 / Structural Seed line 207): PiSugar2 power is a **plugin-host plugin** concern ("power state **also read by the scheduler**, AD-14"). plugin-host is Epic 7 — hence the injected-stub seam now.
- **AD-1**: `core/power.py` is LLM-free and imports no provider/worker code (import-linter KEPT).
- **AD-5**: no new state writes (power isn't persisted); the scheduler reads, never mutates personality state for backoff.

### Testing standards

- `pytest`; **deterministic clock + power injection, never `asyncio.sleep` anchors** (Epic 2 retro #1; 5.1/5.2 rule). The controllable power reader is a tiny test double (`lambda: PowerState(...)` or a mutable holder you flip between battery/plugged).
- New `tests/test_power.py` for the pure policy (truth table + config rejection); extend `tests/test_scheduler.py` for scaled due-ness + `essential` + the EASED/LOW skip matrix + the LIVELY regression; new `tests/test_battery_backoff.py` for the AC3/CAP-10 integration proof.
- **Apply `dev-loop-checklist.md`** before review: tests assert real values (a job's actual next-due time is `T×scale`, not just truthiness) AND exercise every branch (LIVELY/EASED/LOW × reflex/essential-turn/non-essential-turn); reject `scale<1`/NaN/bad-threshold config (the 5.1 cadence-guard precedent); the unknown-charge-on-battery → EASED branch is tested explicitly; no false-positive masking; conftest isolation unchanged (no new write path — call this out).
- Run the **soak** (`-m soak`) locally — green + unchanged (default plugged-in stub ⇒ LIVELY ⇒ identical to 5.2; scheduler parked). Confirm `_seq`/`_bg`/heap unaffected.

### Project Structure Notes

- New: `shelldon/core/power.py`, `tests/test_power.py`, `tests/test_battery_backoff.py`. Modified: `shelldon/core/scheduler.py` (`is_due` gains `scale`; `due`/`tick` thread it; `tick` reads power + applies skip; `Job` gains `essential`; `Scheduler.__init__` gains `power`/`backoff`), `shelldon/core/runtime.py` (backoff defaults + `Core.__init__` params, construct `BackoffPolicy`, inject `power`/`backoff` into `Scheduler`), `tests/test_scheduler.py` (scaled due-ness + `essential` + skip matrix).
- **Unchanged on purpose:** `shelldon/core/budget.py`, `shelldon/core/runtime.py::_dispatch_turn_job`, `shelldon/core/state.py` (no persistence). The arbiter and the 5.2 budget gate are reused as-is.
- LLM-free core (AD-1) stays **KEPT**. No new real-`$HOME` write path (power is read-only, in-RAM).

### Previous-story intelligence (Story 5.2 — review/done; Story 5.1 — done; Story 5.0 — done)

- **5.2 built the turn-dispatch gate and locked the layering** you extend: a due `TURN` job → `_dispatch_turn_job` → arbiter/cooldown/budget. 5.3 inserts the battery skip **upstream** of that, in `tick`, so the existing gate is untouched. Reuse its patterns: pure policy in its own module; fail-fast `not (x>0)`/`not (x>=1)` config validation (rejects NaN); build the mechanism (`essential`) ahead of the consumer (like `cost`); test every branch + reject bad config; guarded best-effort in the driver.
- **5.1 built the scheduler** (cadence + cost tier + the `dispatch_turn` seam + the mark-`last_run`-before-running no-busy-loop rule + per-job guard). 5.3 extends `is_due` with `scale` and `Job` with `essential` — **do not re-invent** the cadence/tier model. 5.1's review lessons (apply preemptively): validate numeric inputs at construction (fail fast, not every tick); pin "no busy-loop on failure/skip" with an explicit test; guard the scaffolding, not just the inner call.
- **5.0 made the turn lifecycle wedge-proof.** A battery-skipped turn never starts a worker, so there is nothing to reap/release — the skip is a pure no-op on the lifecycle. An EASED/LOW essential-vs-non-essential decision only changes *whether* `_dispatch_turn_job` is called; the release-safety invariant is unaffected (no `submit` happens on a skip).
- **No persistence, unlike 5.2.** Resist adding a power field to `PersonalityState` — power is read live each tick; there is nothing meaningful to checkpoint (a stale battery reading across a restart would be worse than a fresh read). Call this out in the completion notes.

### Resolved decisions (owner, 2026-06-18 — binding)

1. **Three backoff levels.** LIVELY (plugged in / charging) → normal cadences, no skip. EASED (on battery, charge ≥ threshold) → stretch + skip non-essential turns. LOW (on battery, charge < threshold) → deeper stretch + skip ALL turns (incl essential). Plugged ⇒ LIVELY regardless of charge.
2. **Cadence stretch applies to ALL job cadences** (reflex + turn). Fewer wakeups is the real battery saving; mood just drifts slower on battery. `Daily` cadence is exempt (once/day); a `Daily` turn job is still subject to the turn-skip.
3. **Defaults (injectable):** `eased_scale = 3.0`, `low_scale = 6.0` (derived: 2× the EASED stretch — a sensible deeper tier; tunable), `low_charge_threshold = 0.20` (20%). Owner chose "3× stretch, low < 20%"; `low_scale = 6.0` is the derived deeper-tier default.
4. **(Forced by Epic 7 ordering)** The real PiSugar2 read is **deferred to Epic 7's plugin-host PiSugar2 plugin**. 5.3 injects a **plugged-in stub reader** (`PowerState(on_battery=False, charge=None)` → LIVELY) and notes the cache-update seam; the policy is built + fully tested now, the hardware read swaps in later with zero policy change.

## References

- [Source: `_bmad-output/planning-artifacts/epics.md`#Story-5.3 (lines 589-607)] — the three ACs verbatim.
- [Source: ARCHITECTURE-SPINE.md] AD-14 (137-140, battery-aware scheduler), AD-9 (112-115, battery-gated turns), AD-8 (110 + Structural Seed 207, PiSugar2 = plugin-host plugin, read by scheduler), AD-1, AD-5.
- [Source: `shelldon/core/scheduler.py`] `Cadence.is_due`/`Interval`/`Idle`/`Daily`, `Scheduler.due`/`tick`, the `Job` model (cost/prompt) — what 5.3 extends.
- [Source: `shelldon/core/runtime.py`] the `Scheduler(...)` construction + the reflex/checkpoint job registration; `_dispatch_turn_job` (the 5.2 gate, left untouched); the `Interval`/`BudgetGate` positive-check + injectable-default precedent.
- [Source: `shelldon/core/budget.py`] the pure policy/driver split + `Decision` enum + `not (x>0)` validation — the shape `core/power.py` mirrors.
- [Source: `shelldon/core/state.py`] read-only confirmation that backoff needs no persistence.
- [Source: `_bmad-output/implementation-artifacts/5-2-cost-tier-gating-and-credit-budget.md`] the gate layering, the "build the mechanism ahead of the consumer" precedent, and the deterministic-test + fail-fast-config lessons.
- [Source: `_bmad-output/implementation-artifacts/5-1-core-scheduler-with-named-multi-cadence-jobs.md`] the cadence/cost-tier model + scheduler review lessons.
- [Source: `_bmad-output/implementation-artifacts/dev-loop-checklist.md`] the pre-review self-checklist.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Amelia / bmad-dev-story)

### Debug Log References

- Full default suite: `uv run pytest -q` → **394 passed, 3 skipped** (platform-gated fork/privdrop), 3 deselected (`live`).
- Soak: `uv run pytest -m soak -q` → **2 passed**, 1 skipped (real-fork, macOS). Unchanged by 5.3 — default plugged-in stub ⇒ LIVELY ⇒ identical scheduler behavior to 5.2; scheduler parked.
- Contracts: `uv run lint-imports` → **2 kept, 0 broken** ("core is LLM-free (AD-1)" KEPT — `core/power.py` imports only stdlib `enum` + `msgspec`).
- New/changed tests: `test_power.py` (10), `test_battery_backoff.py` (5), `test_scheduler.py` (+9: `essential` round-trip, 4× cadence-stretch, 4× tick backoff matrix) = **24 new tests**.

### Completion Notes List

- **New `core/power.py`** (LLM-free, no clock): `PowerState` (RAM-only frozen struct — `on_battery` + `charge: float|None`; default = plugged-in stub), `BackoffLevel` (LIVELY/EASED/LOW), `BackoffPolicy` (pure policy: `level` → `cadence_scale` + `skips`). Plugged ⇒ LIVELY regardless of charge; on-battery + known charge < threshold ⇒ LOW; else (ample **or unknown** charge) ⇒ EASED — a missing reading never escalates to the deepest backoff. Mirrors `budget.py`'s policy/driver split.
- **Cadence stretch (`scheduler.py`):** `Cadence.is_due` gained a `scale: float = 1.0`; `Interval`/`Idle` multiply their period by it (stretch); `Daily` ignores it (once/day). `Scheduler.due` threads `scale`. Default `1.0` keeps every pre-5.3 call behavior-identical.
- **Turn-skip + `Job.essential` (`scheduler.py`):** `Job` gained `essential: bool = False` (mechanism, like 5.2's `cost`). `tick()` reads power ONCE per pass → level → applies the stretch (via `due(scale=...)`) and, for **turn-tier** jobs only, skips per `backoff.skips(level, essential=...)`. **Reflex jobs are stretched but never skipped** (cheap, no LLM, carry aliveness).
- **Gate layering honored:** the battery skip is decided in the scheduler **before** the `dispatch_turn` hook is called, so it is an OUTER gate over the 5.2 budget gate — `_dispatch_turn_job` is **untouched**. A battery-skipped turn never spawns and never spends budget (proven by `test_on_battery_due_non_essential_turn_is_skipped`).
- **Runtime wiring (`runtime.py`):** `BackoffPolicy` constructed from new injectable defaults (`eased_scale=3.0`, `low_scale=6.0`, `low_charge_threshold=0.20` — owner decision 3); `power=` injectable (default plugged-in stub). Both passed into `Scheduler(...)`. Config validation delegated to `BackoffPolicy` (like `Interval()`/`BudgetGate`).
- **No persistence (unlike 5.2):** power is read live each tick; `state.py` is **unchanged**. A stale battery reading across a restart would be worse than a fresh read — nothing to checkpoint. Confirmed no new real-`$HOME` write path ⇒ **no conftest change**.
- **Real PiSugar2 read deferred to Epic 7** (plugin-host PiSugar2 plugin, AD-8). The injected stub seam swaps for the real cached reading with zero policy change; the reader contract is "non-blocking cached value" (documented in `Scheduler`/`Core`).
- **dev-loop-checklist applied:** config rejects `scale<1`/NaN + threshold out-of-range/NaN; tests assert real values (`scale == 3.0`, `turns_used == 0/1`, due membership) and exercise every branch (LIVELY/EASED/LOW × reflex/essential/non-essential, unknown-charge→EASED, skip-advances-`last_run` no-busy-retry); negative checks (skip ⇒ no spawn AND no spend). **Known minor dup:** `_RecordingSpawner` (5 lines) is duplicated from `test_turn_dispatch.py`; extracting it to conftest would touch 5.2's test file (out of this story's surgical scope) — folded into the existing `test-hygiene-burndown` icebox item.

### File List

- `shelldon/core/power.py` (new — `PowerState`/`BackoffLevel`/`BackoffPolicy`)
- `tests/test_power.py` (new)
- `tests/test_battery_backoff.py` (new — AC3/CAP-10 integration)
- `shelldon/core/scheduler.py` (modified — `is_due` gains `scale`; `due` threads it; `Job` gains `essential`; `Scheduler.__init__` gains `power`/`backoff`; `tick` reads power + applies stretch/skip)
- `shelldon/core/runtime.py` (modified — backoff defaults + `Core.__init__` params, construct `BackoffPolicy`, inject `power`/`backoff` into `Scheduler`)
- `tests/test_scheduler.py` (modified — `essential` + cadence-stretch + tick backoff matrix)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (status tracking)

## Change Log

| Date | Change |
|------|--------|
| 2026-06-18 | Story 5.3 implemented: battery-aware backoff (AD-14/AD-9). New `core/power.py` (`BackoffPolicy`: 3-tier LIVELY/EASED/LOW from a `PowerState` reading → cadence stretch + turn-skip). Scheduler stretches all `Interval`/`Idle` job cadences (×3 EASED / ×6 LOW) and skips non-essential (EASED) / all (LOW) turn jobs; reflexes stretched but never skipped. `Job` gains `essential` flag (mechanism). Battery is an OUTER scheduler gate over 5.2's UNTOUCHED `_dispatch_turn_job`. Real PiSugar2 read deferred to Epic 7 (injected plugged-in stub now); no persistence. +24 tests; suite 394 pass / soak 2 pass; contracts KEPT. Owner decisions locked: 3 tiers, stretch all cadences, eased×3/low×6/threshold 20%. |
| 2026-06-18 | Code-review follow-ups (2 Patches) resolved: `BackoffPolicy.__init__` now cross-validates `low_scale >= eased_scale` (a smaller deepest-tier scale would invert the battery-saving contract); +`charge=0.0` drained-boundary → LOW test. +2 tests. Suite 396 pass / soak 2 pass; contracts KEPT. 10 Defer items accepted (pre-existing 5.1/5.2 items, Epic 7 plugin-boundary input validation, minor test-style gaps). |

## Review Findings

- [x] [Review][Patch] `low_scale < eased_scale` not cross-validated in `BackoffPolicy.__init__` — if `low_scale < eased_scale` (e.g. `low=1.5, eased=3.0`), LOW-level cadence fires MORE often than EASED, inverting the battery-saving contract. **FIXED**: added `if not (low_scale >= eased_scale): raise ValueError(...)` after the individual scale guards (`core/power.py`); +`test_rejects_low_scale_below_eased_scale` (rejects `low<eased`, allows equal). [`shelldon/core/power.py`]
- [x] [Review][Patch] Missing test: `charge=0.0` on battery → LOW — the fully-drained boundary is not explicitly covered; only `charge=0.10` tests LOW. A future boundary-condition change would silently invert to EASED. **FIXED**: +`test_fully_drained_on_battery_is_low` (`charge=0.0` ⇒ LOW). [`tests/test_power.py`]
- [x] [Review][Defer] `Daily.is_due` UTC-day vs local-day TZ strip [`shelldon/core/scheduler.py:117`] — pre-existing from 5.1; already in deferred-work.md
- [x] [Review][Defer] Budget rollover clock-skew: `now` at `evaluate` vs `admission_patch` differs across midnight [`shelldon/core/budget.py:77-100`] — extremely rare; deferred
- [x] [Review][Defer] Silent permanent SKIP when `job.cost > daily_turn_budget` — no diagnostic at SKIP time [`shelldon/core/budget.py:82`] — deferred, diagnostic gap only
- [x] [Review][Defer] `Idle.is_due` exact-timestamp re-fire (`last_run <= last_interaction`) [`shelldon/core/scheduler.py:93`] — pre-existing from 5.1; cosmically rare
- [x] [Review][Defer] `PowerState.charge` accepts negative values → forced LOW from bad hardware [`shelldon/core/power.py:23-31`] — deferred to Epic 7 plugin boundary
- [x] [Review][Defer] Missing test: `eased_scale=1.0` is a valid accepted input [`shelldon/core/power.py:46`] — deferred, guard logic clear
- [x] [Review][Defer] `turns_used > daily_turn_budget` after config decrease → SKIP until rollover [`shelldon/core/budget.py:77`] — low operational risk; deferred
- [x] [Review][Defer] `apply_patch` after `arbiter.submit()` with no explicit rollback [`shelldon/core/runtime.py:465-471`] — budget.* paths can't raise; turn timeout recovers slot; deferred
- [x] [Review][Defer] `_scheduler_loop` passes no `last_interaction` to `tick()` — acknowledged 5.4 deferral [`shelldon/core/runtime.py`]
- [x] [Review][Defer] Hardcoded date in `test_cadence_stretch_is_demonstrable_on_battery` [`tests/test_battery_backoff.py:125`] — style gap only, no functional impact
