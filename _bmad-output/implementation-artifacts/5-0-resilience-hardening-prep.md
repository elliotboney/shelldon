---
baseline_commit: a52a14eafdcc4ae1675542b06414acd5301bfa39
---
# Story 5.0: Resilience hardening prep — the turn lifecycle never wedges

Status: done

<!-- Retro-born story (Epic 4 retro, 2026-06-18). NOT in epics.md — it gates Epic 5's scheduler. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the turn lifecycle to always release its ≤1-worker slot and never hang — even when a reply fails to send, a worker stalls, a fork fails, or a child won't die,
so that Epic 5's scheduler can submit autonomous turn-jobs on a cadence without inheriting a path that freezes the pet for ~90s (or forever).

**Why this is a prep story (gates 5.1):** Epic 5's scheduler (AD-14) proposes cadence-driven turn-jobs that go through the **same arbiter and the same ≤1-worker bound** as every chat turn (AD-9). Today the turn lifecycle has a proven **~90-second wedge** and several "block forever" paths. A scheduler firing turns into that is a reliability multiplier on a latent bug. Harden the lifecycle first, then build the scheduler on solid ground.

## Acceptance Criteria

### AC1 — The in-flight slot ALWAYS releases (kill the ~90s wedge)

**Given** a turn whose `Result` arrives but whose reply/degrade delivery fails (`bus.deliver` raises `OSError`)
**When** core processes the result in `_handle_result`
**Then** `arbiter.complete()` still runs (the slot is released and any pending catch-up is folded) — a failed reply send must never leave the arbiter slot reserved forever.

**Given** a worker that stalls past core's turn timeout (core degrades, closes the fence) while the previous fork has not yet reaped
**When** core attempts the next turn and `spawn_turn` raises `WorkerBusyError`
**Then** the system does NOT enter the repeating `submit → WorkerBusyError → reset → submit` loop that locks new turns for ~90s — the arbiter's admission and the fork-server's `worker_in_flight` guard cannot diverge into a freeze.

> **Root cause (confirmed, cite in implementation):** `Arbiter.worker_in_flight` (`shelldon/core/arbiter.py:19`) and `ForkServer.worker_in_flight` (`shelldon/worker/forkserver.py:124`) are **two independent booleans** with no mutual exclusion. When core's 30s timeout fires but the worker holds its fork until the 120s completion timeout, the arbiter resets and re-admits while the fork-server is still locked → `WorkerBusyError` on every retry until the real reap ~90s later. **Fix the divergence** (single source of truth, or sequence admission so the arbiter cannot re-admit while a fork is still reaping) **and** guarantee `arbiter.complete()` runs on every result path.

### AC2 — Timeouts are coherent (no asymmetry wedge, no infinite outbound block)

**Given** the worker completion timeout (`_COMPLETION_TIMEOUT_S = 120.0`, `worker/worker.py:52`) and core's turn timeout (`DEFAULT_TURN_TIMEOUT = 30.0`, `runtime.py:60`)
**When** a worker's broker goes silent
**Then** the worker fails its Result within a window **aligned to** core's reap horizon — a worker must not hold the fork ~90s past core's degrade. Either reduce `_COMPLETION_TIMEOUT_S` toward core's timeout, or make both injectable from one config so the relationship `worker_timeout ≤ core reap horizon` is explicit and tested (no magic 4× gap).

**Given** the outbound `Result → core` write in `run_worker` (`worker/worker.py:136-146`) which today has **no timeout**
**When** the hub/core stalls and stops reading
**Then** the worker's `write_frame` is bounded by a timeout (mirror the inbound `asyncio.wait_for` pattern at `worker/worker.py:85`) and the worker exits rather than blocking forever past its window.

### AC3 — The fork / reap / preload path is robust

**Given** `os.fork()` in `_os_fork_spawn` (`forkserver.py:77`) which today does **not** catch `OSError`
**When** the kernel refuses the fork (ENOMEM/EAGAIN)
**Then** the failure surfaces as a failed turn (degrade-to-reflex, AD-9) — core does not crash on an unhandled `OSError`.

**Given** `_os_waitpid_reap` / `reap_current` (`forkserver.py:88-97`, `174-187`) which today loop with **no timeout**
**When** a child is unkillable (wedged in a kernel sleep)
**Then** reaping is bounded with a **SIGKILL escalation**, and the ≤1 guard still releases (the `finally: worker_in_flight = False` invariant holds) — an unreapable child can never spin forever or lock all future turns.

**Given** the fork child (`forkserver.py:78-84`) which today inherits parent FDs and exits 0 on any failure
**When** the child starts
**Then** it closes inherited FDs (`os.closerange(3, …)`) before `asyncio.run`, AND a failed job is **visible** (log before `_exit`, and/or non-zero exit) rather than a silent `os._exit(0)`.

**Given** `preload()` (`forkserver.py:136-151`) which calls `gc.disable()` before importing
**When** an import raises
**Then** GC is re-enabled (try/finally) — a preload failure must not leave the parent process with GC permanently disabled.

### AC4 — Arbiter admission is release-safe and its invariant is explicit

**Given** the arbiter's `submit()` admission (`arbiter.py:31-42`) — serial single-consumer by design
**When** any exception fires on the spawn path between admit and the fork
**Then** the reserved slot is guaranteed to release (the existing `arbiter.reset()` path at `runtime.py:182` is preserved and the "every admit has a guaranteed release" invariant is enforced by a try/finally seam and a test). Document the single-consumer invariant in `arbiter.py`. **Do NOT add an `asyncio.Lock`** unless a genuine concurrent caller is introduced — the design is provably serial (one core loop calls `submit`); the real defect was the dual-boolean divergence (AC1), not a data race.

### Out of scope (explicit — do NOT build here)

- **Broker reconnect loop / `run_broker` supervisor** (`1-4` deferred) — resilience of the broker process, not the turn lifecycle.
- **`app.py` multiprocess child supervision** (`4-3` deferred: `child.join(timeout=5.0)` zombies, mid-loop `child.start()` failure, `preload()`-after-`ensure_vault` cleanup) — **deploy-time** hardening, Pi bring-up.
- **Redelivery of the dropped catch-up prompt on `WorkerBusyError`** (`runtime.py:178-179`) — accepted degradation; Epic 2 deferred, stays deferred.
- **`connect()` timeout / retry backoff** — Epic 2 territory; the chain already has retry/fallback.
- Any **scheduler / cadence / battery** code — that is 5.1+.

## Tasks / Subtasks

- [x] **Task 1 — Kill the wedge (AC1)**
  - [x] In `_handle_result` (`runtime.py`), wrapped the reply/degrade delivery in try/except so `arbiter.complete()` runs on every path. If `bus.deliver` raises, log + still release the slot + still fold pending. Same guard added to `_timeout_watch`.
  - [x] Eliminated the `Arbiter`/`ForkServer` `worker_in_flight` divergence at its root: the coherent-timeout chain (Task 2, W < R < T) means the worker self-reports + is reaped BEFORE core's degrade, so the arbiter never re-admits while a fork is still held. The bounded reap (Task 3) is the backstop for a truly-wedged child. AD-9 (≤1) and AD-12 (fence) unchanged.
  - [x] Tests: (a) `_send_reply` raises → slot released, fence idle; (b) catch-up still flushes after a failed reply; (c) timeout-path degrade raises → slot still released.
- [x] **Task 2 — Coherent timeouts (AC2)**
  - [x] Lowered `_COMPLETION_TIMEOUT_S` 120.0 → 25.0 (< core's 30.0); documented the W < R < T invariant in all three modules and added a guard test.
  - [x] Added `asyncio.wait_for(_RESULT_WRITE_TIMEOUT_S)` around the outbound `Result → core` `write_frame`; on timeout the worker logs + exits cleanly.
  - [x] Tests: timeout-chain invariant asserted; outbound write to a non-reading core returns within the bound instead of hanging.
- [x] **Task 3 — Robust fork/reap/preload (AC3)**
  - [x] Catch `OSError` around `os.fork()` → raise `RuntimeError` with context; core's existing spawn-failure handler releases the guards (no crash).
  - [x] Bounded `_os_waitpid_reap` with `_REAP_TIMEOUT_S` + SIGKILL escalation (injectable os calls); kept the `finally: worker_in_flight = False` guard. Abnormal child exits are now logged (visible failure).
  - [x] Child: `os.closerange(3, SC_OPEN_MAX)` before drop+run; non-zero `os._exit(1)` on failure instead of silent `os._exit(0)`.
  - [x] `preload()`: re-enable GC on import failure (success keeps it disabled by design for COW).
  - [x] Tests: fork-OSError → RuntimeError; unkillable child → SIGKILL + reclaim; natural exit → no kill; abnormal exit logged; preload import error → GC re-enabled.
- [x] **Task 4 — Release-safe arbiter (AC4)**
  - [x] Verified every `submit()` admission has a guaranteed release (`complete` always-runs from Task 1, or `reset` on spawn failure — both already wired). No `asyncio.Lock` added.
  - [x] Documented the release-safety + single-consumer invariant in `arbiter.py`.
  - [x] Test: spawn-failure path → arbiter slot released, fence idle.
- [x] **Task 5 — Full-suite + contracts**
  - [x] Full suite green: **318 passed, 3 skipped, 3 deselected** (was 307; +11). Both import-linter contracts **KEPT** (`core is LLM-free`, `transport holds no creds`). No new `$HOME` write paths (existing conftest isolation covers all core writers).

## Dev Notes

**This story hardens code shipped in Epic 1 (1.5 fork-server, 1.8 turn loop) and Epic 4 (4.5 worker-emits-Result topology).** The 4.5 reshape made the worker live longer (Job → await Completion → emit Result), which is what *exposed* these block-forever paths — see the 4.5 retro note. You are not adding features; you are closing wedge/hang paths in an existing, working system. **Leave the system working end-to-end** — every change must preserve AD-9 (≤1 worker), AD-12 (turn_id fence + idempotent close), and the degrade-to-reflex guarantee (the pet never freezes).

### Files to touch (all UPDATE — read each fully before editing)

- `shelldon/core/runtime.py` — `_handle_result` (187-206), `_timeout_watch` (218-233), `_start_turn` (172-183), timeout constants (`DEFAULT_TURN_TIMEOUT=30.0` @60). The unguarded `await self._send_reply(...)` / `await self._degrade()` before `arbiter.complete()` (204) is the wedge's reply-side root.
- `shelldon/core/arbiter.py` — `submit` (31-42, NOT async-safe but serial-by-design), `complete` (44-58), `reset` (60-71). Two booleans diverge from the fork-server's.
- `shelldon/worker/forkserver.py` — `_os_fork_spawn` (63-85, bare `os.fork()` @77, child `finally: os._exit(0)` @78-84, no `closerange`), `_os_waitpid_reap` (88-97, `while True` no timeout), `reap_current` (174-187, no reap timeout), `worker_in_flight` (set @165, cleared @187), `preload` (136-151, `gc.disable()` not re-enabled on import error).
- `shelldon/worker/worker.py` — `_COMPLETION_TIMEOUT_S=120.0` (@52, used @85), outbound `write_frame` Result→core (136-146, **no timeout**), `_result_from_broker` (80-98).

### Current-state map (what exists today; what must be preserved)

| Path | Current behavior | Preserve | Change |
|---|---|---|---|
| `runtime.py:204` `arbiter.complete()` | called only if reply/degrade didn't raise | the catch-up fold + slot release | make it run on EVERY result path |
| `arbiter.py:19` + `forkserver.py:124` | two independent `worker_in_flight` | the ≤1 bound (AD-9) | remove the divergence |
| `forkserver.py:187` `finally: worker_in_flight=False` | guard always releases on reap | this invariant — keep it | add reap timeout/SIGKILL above it |
| `forkserver.py:77` `os.fork()` | OSError uncaught → core crash | one-fork-per-turn (AD-3) | catch OSError → degrade |
| `forkserver.py:78-84` child | silent `os._exit(0)`, FDs inherited | RAM reclaim via `_exit` | closerange + visible failure |
| `worker/worker.py:85` inbound read | `asyncio.wait_for(_COMPLETION_TIMEOUT_S)` | the bounded-read pattern | mirror it on the outbound write (136-146) |
| `runtime.py:190-193` fence | `accept` + `close` (AD-12) | turn_id fencing exactly | unchanged |

### Architecture invariants (binding — must hold after hardening)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-9** (lines 112-115): "**≤1 worker turn in flight**; events during a turn **coalesce into a single pending catch-up slot** … on provider-chain exhaustion the arbiter **falls back to a reflex behavior** so the pet never freezes." The ≤1-worker bound is a **required M0 test**.
- **AD-12** (lines 127-130): "every turn carries a `turn_id`; core **fences** on it. A `Result` whose `turn_id` is already closed … is **discarded**. Turn close is **idempotent**."
- **AD-3** (lines 78-81): "fork-server parent … `os.fork()`s one worker per turn; the worker dies after its turn and its RAM is reclaimed. **At most one worker in flight**." COW needs `gc.disable()` + `gc.freeze()` before fork.
- **AD-14** (lines 137-140, the consumer of this story): "Scheduler-proposed turn jobs go through the **arbiter** (AD-9) — same ≤1-worker bound, coalescing, and credit/battery gate as every other turn; the scheduler never forks directly." → 5.1 builds on exactly the lifecycle you're hardening.
- Consistency convention (line 154): "a turn failure degrades to reflex, never blocks."

### Testing standards

- `pytest`; **state-predicate polling, never `asyncio.sleep` anchors** (Epic 2 retro #1 — use the shared `await_true`/conftest helpers).
- Inject the failure: stub a `bus.deliver` that raises; a `spawn` that raises `OSError`; a `reap` that never returns (to exercise the SIGKILL escalation) — mirror the existing injectable `spawn=`/`reap=`/`drop=` seams on `ForkServer`.
- **Apply `dev-loop-checklist.md`** (`_bmad-output/implementation-artifacts/dev-loop-checklist.md`) before requesting review: best-effort paths guarded (the whole point of this story), tests assert real values + exercise the failure branches (not just happy path), no false-positive masking, conftest isolation in the same change.
- Real OS-denial / SIGKILL tests that need Linux semantics: gate with `skipif` and log the reason — never fake green (4.3 precedent).

### Project Structure Notes

- No new modules. All edits land in existing `core/runtime.py`, `core/arbiter.py`, `worker/forkserver.py`, `worker/worker.py`. Tests extend `tests/test_turn_fence.py`, `tests/test_forkserver*.py`, `tests/test_end_to_end_turn.py`, and/or a new `tests/test_resilience.py` if cleaner.
- LLM-free core contract (AD-1) must stay **KEPT** — no provider imports leak into `core/`.

### Previous-story intelligence (Epic 4 — the code you're hardening)

- **4.5 (worker-emits-Result)** is what surfaced the outbound-write and reap-wedge risks: "Fire-and-forget topology hid resilience issues. The worker now lives longer … exposing what happens when the broker goes silent. The timeout backstop is mandatory." 4.5 added the inbound `_COMPLETION_TIMEOUT_S` backstop but explicitly deferred the **outbound** write timeout and the **120/90/30s asymmetry** to a resilience story — this one.
- **Recurring review classes to self-apply (don't make me catch them again):** missing exception guards on best-effort paths (4.1/4.4/4.5), tests that assert truthiness or have false-positive masking (4.4 CAP-6), missing rejection/failure-branch tests (3.4). This story is *entirely* about failure branches — test them explicitly.

### References

- [Source: `_bmad-output/implementation-artifacts/epic-4-retro-2026-06-18.md`#Significant-discovery → Epic 5] — Story 5.0 carve + the resilience debt list.
- [Source: `_bmad-output/implementation-artifacts/deferred-work.md`] — exact deferred items: 4.5 outbound `write_frame` no timeout + 120/90/30s asymmetry; 1.5 fork-child exit-0 / `_os_waitpid_reap` no timeout / child FD inheritance / `Arbiter.try_begin` async-safety / `gc.disable()` not re-enabled; 4.3 `os.fork()` OSError uncaught.
- [Source: ARCHITECTURE-SPINE.md] AD-3 (78-81), AD-9 (112-115), AD-12 (127-130), AD-14 (137-140), consistency conventions (154).
- [Source: sprint-status.yaml] `5-0-resilience-hardening-prep` — GATES 5.1.

## Review Findings

*(Code review 2026-06-18 — 3 layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor)*

### Decision-Needed

- [x] [Review][Decision] AC1 residual divergence window → **RESOLVED: structurally fixed, not accepted.** The reviewer is right that the timeout chain only *bounded* the divergence and AC1 explicitly says "Fix the divergence." Fixed by sequencing: a turn end now **awaits the reap (`_await_reap`) BEFORE `arbiter.complete()`**, so the fork-server guard and arbiter slot release in lockstep — a catch-up turn can never hit a freed arbiter while the fork is still held. No residual window. Analyzed the loop-block risk: `_await_reap` is bounded by the reap's SIGKILL deadline (R<T) and a worker that emitted a Result exits at once, so the common case returns immediately; a block would require a child that sends a Result then refuses `os._exit` (only via external SIGSTOP — not an operational case). Proven by `test_reap_runs_before_the_catch_up_spawn`.

### Patches

- [x] [Review][Patch] Blocking `waitpid(handle, 0)` after SIGKILL → replaced with a bounded WNOHANG poll (`_REAP_KILL_GRACE_S`); a D-state child can no longer freeze the loop — poll, then leave the zombie to the OS [`forkserver.py:_os_waitpid_reap`]
- [x] [Review][Patch] `except Exception` too wide → narrowed: the try now wraps ONLY the bus delivery; `_apply_proposed_ops`/`_record_turn` (self-guarded) run after it, so a delivery failure no longer skips them and the broad except can't mask their bugs [`runtime.py:_handle_result`]
- [x] [Review][Patch] `os.closerange` SC_OPEN_MAX → capped at `min(SC_OPEN_MAX, _MAX_INHERITED_FD=4096)` [`forkserver.py:_os_fork_spawn`]
- [x] [Review][Patch] `_RESULT_WRITE_TIMEOUT_S` 25→5.0 (strictly < the 25s broker-read window), so a reply landing near t=25 isn't cut off mid-write by the reaper [`worker.py`]
- [x] [Review][Patch] `gc.collect()`/`gc.freeze()` moved INSIDE the guard — any failure in the gc-managed warm-up re-enables GC before propagating [`forkserver.py:preload`]
- [x] [Review][Patch] catch-up `_start_turn` face push → guarded `_push_face(FACE_THINKING)` inside `_start_turn`; a cosmetic face failure can no longer propagate to `run()` and tear down the core loop [`runtime.py:_start_turn`]
- [x] [Review][Patch] test now asserts strict `_RESULT_WRITE_TIMEOUT_S < _COMPLETION_TIMEOUT_S` [`tests/test_resilience.py`]
- [x] [Review][Patch] `test_outbound_result_write_is_bounded` now asserts the worker completes (sets `completed=True` after the await) — an exception would fail, not pass [`tests/test_resilience.py`]
- [x] [Review][Patch] Added `test_handle_result_releases_slot_when_degrade_raises` (ok=False + degrade-send fails) [`tests/test_resilience.py`]

### Deferred

- [x] [Review][Defer] `test_fork_oserror_becomes_runtime_error` patches `os.fork` globally — works now, fragile if import style changes [`tests/test_resilience.py:233`] — deferred, pre-existing pattern; works correctly

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- `rtk` shell hook intercepts `pytest`/`python` and reports "No tests collected"; ran the venv binary directly (`.venv/bin/python -m pytest`). `lint-imports` must be on PATH for `test_core_is_llm_free` (ran `PATH="$PWD/.venv/bin:$PATH" …`).

### Completion Notes List

- **Root-cause fix, not a patch:** the ~90s wedge came from the worker completion timeout (120s) sitting 4× above core's degrade (30s) — a stalled worker held the fork ~90s past core giving up, and the dual `worker_in_flight` booleans then looped `submit → WorkerBusyError → reset`. Inverting the timeout ordering to **W(25) < R(28) < T(30)** makes the worker self-report a failure Result *before* core abandons the turn, so the arbiter slot and fork-server guard release in lockstep. The bounded reap (SIGKILL at R) is the backstop for a truly-wedged child that can't self-report.
- **AD-1 boundary respected:** `core/runtime.py` cannot import `worker/` (import-linter). So the slot-release guarantee lives entirely in core (`_handle_result` / `_timeout_watch` always reach `arbiter.complete()`), and the timeout coherence is enforced by a documented cross-module invariant + a guard test — not by importing worker constants into core.
- **Scope note on AC3 "degrade-to-reflex" for fork-OSError:** implemented as *graceful failure* (OSError wrapped → core releases guards, no crash, lifecycle continues) rather than a user-facing degrade ack. Distinguishing a fork failure from the (now near-dead) `WorkerBusyError` coalescing race inside core would require importing `WorkerBusyError` from `worker/` (AD-1 violation). If a user-facing ack on fork failure is wanted, the clean follow-up is to move `WorkerBusyError` into a shared `core/`-importable module — flagged, not done here.
- **Self-applied `dev-loop-checklist.md`:** every change is a guarded best-effort path; all 11 new tests assert real values and exercise the *failure* branches (raise-on-deliver, SIGKILL, OSError, import failure), not happy paths; no false-positive masking; isolation via existing fixtures.

### Change Log

- 2026-06-18: Implemented Story 5.0 — coherent timeout chain (W<R<T), guaranteed ≤1-slot release in `_handle_result`/`_timeout_watch`, bounded reap with SIGKILL escalation, fork-OSError handling + child FD `closerange` + visible non-zero exit, `preload()` GC re-enable on failure, arbiter release-safety doc. +11 tests (`test_resilience.py`). Suite 318 pass / 3 skip; contracts KEPT.
- 2026-06-18: Addressed code review — 1 decision + 8 patches resolved. **Structural AC1 fix:** turn end awaits the reap before releasing the arbiter (`_await_reap`), eliminating the residual divergence window. Patches: non-blocking post-SIGKILL reclaim, narrowed `_handle_result` try, capped `closerange`, write timeout 25→5s, GC guard covers collect/freeze, guarded turn-start face push, 3 test tightenings. +2 net tests (13 in `test_resilience.py`). Suite 320 pass / 3 skip; contracts KEPT.

### File List

- `shelldon/core/runtime.py` (modified) — guarded `_handle_result` + `_timeout_watch`; coherent-timeout invariant doc on `DEFAULT_TURN_TIMEOUT`.
- `shelldon/core/arbiter.py` (modified) — release-safety + single-consumer invariant doc (AC4).
- `shelldon/worker/worker.py` (modified) — `_COMPLETION_TIMEOUT_S` 120→25, new `_RESULT_WRITE_TIMEOUT_S`, bounded outbound Result write.
- `shelldon/worker/forkserver.py` (modified) — `_REAP_TIMEOUT_S`; bounded `_os_waitpid_reap` w/ SIGKILL + abnormal-exit log; `os.fork()` OSError→RuntimeError; child `closerange` + non-zero exit; `preload()` GC re-enable on import failure.
- `tests/test_resilience.py` (new) — 11 tests across AC1–AC4.
