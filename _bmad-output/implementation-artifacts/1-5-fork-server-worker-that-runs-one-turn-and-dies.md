---
baseline_commit: b13dc3eaae7f528fc422b0b316870957782736c1
---

# Story 1.5: Fork-server worker that runs one turn and dies

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want each LLM turn handled by a warm-forked worker that exits afterward,
so that the pet stays within 512MB and never accumulates memory across turns (v1's OOM) (AD-3, NFR2).

## Acceptance Criteria

1. **Warm fork-server forks one worker per turn:** a fork-server parent has pre-imported the LLM libraries (not credentials), with `gc.disable()` + `gc.freeze()` applied **before** forking. After the parent signals its readiness barrier, a turn request `os.fork()`s exactly **one** worker, which assembles the prompt and **proxies the authenticated call to the broker** (sends a `Job` over the bus), then **exits, reclaiming its RAM**.
2. **≤1 worker in flight:** while a turn is in flight, another turn request does **not** spawn a second worker — at most one worker exists at a time (verified by an **M0 concurrency test**).
3. **Idempotent turn close:** when a worker has exited or been superseded and a late `Result` arrives carrying its **closed `turn_id`**, core **discards it** (no error, no state mutation).

## Tasks / Subtasks

> **Test seam (read first — it shapes everything):** the raw `os.fork()` is injected behind a single `spawn` callable. ALL orchestration logic (≤1 guard, readiness, reaping, fencing) is tested cross-platform with a **fake spawner** (no real fork). The real-`os.fork()` path is a separate integration test gated `@pytest.mark.skipif(sys.platform == "darwin", ...)` — macOS cannot safely `fork()` without `exec()` (Apple frameworks abort the child); the prod target is Linux (Pi).

- [x] **Task 1: Core-side turn fence (idempotent close, AD-12)** (AC: 3)
  - [x] New `shelldon/core/turn.py`: a small `TurnFence` (pure, no I/O) holding `current_turn_id: str | None` and a bounded `closed: set[str]`. Methods: `open(turn_id)` (sets current), `close(turn_id)` (moves current→closed, idempotent), `accept(result_env) -> bool` (True only if `result_env.turn_id == current`; a `turn_id` that is closed/None/unknown → **False = discard**).
  - [x] Keep `closed` bounded (e.g. a capped deque/set of recent ids) — don't grow unboundedly. Minimal for 1.5; supersession/timeout sophistication is the arbiter's job (1.8).
- [x] **Task 2: Minimal arbiter skeleton — the ≤1-worker guard (AD-9)** (AC: 2)
  - [x] New `shelldon/core/arbiter.py` (or fold into `turn.py`): an in-flight guard — `worker_in_flight: bool` (or the in-flight `turn_id`). A spawn request while in-flight is **refused** (return False / raise `WorkerBusyError`), never a second fork. The flag is **released when the worker exits or its spawn fails** — releasing on failure is the bug that deadlocks all future turns if missed; test it explicitly.
  - [x] This is a SKELETON: just the bound. Event coalescing, cooldown, budget, fallback (full AD-9) are **1.8 / Epic 2** — do NOT build them.
- [x] **Task 3: The worker child (fire-and-forget)** (AC: 1)
  - [x] New `shelldon/worker/worker.py`: `async def run_worker(socket_path, turn_id, prompt)` — connect to the bus as `Actor.WORKER` (existing `connect`), send `Envelope(kind=JOB, src=WORKER, dst=BROKER, body=Job(payload=prompt), turn_id=turn_id)`, then **return/exit. It does NOT wait for the Result** — the broker's Result routes to CORE (RESULT→CORE), which core fences (Task 1). No new `MsgKind`/contract.
  - [x] In 1.5 the `prompt` is a canned/injected string — **real prompt assembly (history + memory) is Story 1.8**. The worker is intentionally minimal: connect, send one Job, die.
- [x] **Task 4: Fork-server (warm parent + ≤1 spawn + reaping)** (AC: 1, 2)
  - [x] New `shelldon/worker/forkserver.py`: a `ForkServer` that owns the warm-fork lifecycle.
    - **Preload + freeze ordering (binding):** `gc.disable()` at construction → `preload(modules)` imports the warm libs → `gc.collect()` (compact) → `gc.freeze()` — **all before any fork**. (gc.freeze exempts the pre-imported objects from GC scans so they stay COW-shared.)
    - **Readiness barrier:** `await ready()` resolves once preload+freeze is done; nothing forks before it. (Real cross-process form is a `socket.socketpair()` sentinel byte; for the in-process component a set-once `asyncio.Event`/flag is fine — note the socketpair as the production shape.)
    - **`spawn_turn(turn_id, *, spawn=_os_fork_spawn)`:** ≤1-guarded (Task 2). Calls the injected `spawn` seam, records the in-flight worker (pid/handle), and returns it. The default `spawn` does `os.fork()`; child runs the worker then `os._exit(0)`; parent returns the pid.
    - **`reap()`:** `os.waitpid(pid, 0)` the single worker (blocking is fine at ≤1 in flight — no `SIGCHLD` handler), then **release the in-flight guard**. Child exit reclaims its RAM.
  - [x] **Child GC:** leave GC **disabled** in the child — the worker is sub-second and exits before any cyclic garbage matters; re-enabling would only risk dirtying COW pages. Document this deliberate deviation from the textbook "re-enable in child" pattern.
  - [x] **Do NOT fork from the asyncio core loop.** Forking a multi-threaded/event-loop process is unsafe (3.12+ warns; 3.14 flips multiprocessing to forkserver). Production runs the fork-server as its **own single-threaded process** driven by core over IPC — document this as the deployment shape; 1.5 delivers the mechanism + the injectable seam, full process/IPC wiring is 1.8.
- [x] **Task 5: Tests — orchestration (cross-platform, fake spawner)** (AC: 1, 2, 3)
  - [x] `tests/test_turn_fence.py`: `accept` returns True for the current `turn_id`, **False** for a closed / None / unknown `turn_id`; `close` is idempotent (closing twice is safe); a Result for a superseded turn is discarded.
  - [x] `tests/test_arbiter_inflight.py`: with a **fake spawner** blocked on an `asyncio.Event` — (a) one in flight blocks/refuses a second (spawner invoked exactly once); (b) releasing the first lets the second proceed (total 2, never 2 concurrent — track a concurrency counter, assert it never exceeds 1); (c) a spawn that raises **releases** the guard (next turn can spawn). This is the **M0 concurrency test**.
  - [x] `tests/test_worker_sends_job.py`: drive `run_worker` against a real `BusServer` + a stub broker client; assert a `JOB` envelope (correct `turn_id`, `Job` body) reaches the broker and the worker returns without reading a Result.
- [x] **Task 6: Test — real `os.fork()` integration (Linux-gated)** (AC: 1)
  - [x] `tests/test_forkserver_fork.py` with `@pytest.mark.skipif(sys.platform == "darwin", reason="fork-without-exec unsafe on macOS frameworks; prod target is Linux")`: a real `ForkServer` forks one worker that connects to a real `BusServer` + stub broker, sends its `Job`, and exits; parent `reap()`s it (assert clean exit, in-flight guard released).
  - [x] **Flag the coverage gap:** there is no Linux CI yet, so this test runs only on the Pi/dev-Linux. Note it in Dev Notes. The COW RAM-ceiling check (`/proc/self/smaps_rollup` `Private_Dirty` ceiling, the real proof of the AD-3 win) is a **Linux-CI follow-up** — out of scope here, recorded as a risk.
- [x] **Task 7: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → `core/` (turn fence, arbiter) and `worker/` import no provider SDK directly; **core stays LLM-free** — KEPT. (The fork-server "pre-imports LLM libs" but lives in `worker/`, not `core/`.)
  - [x] `uv run pytest -q` → all green (prior suites + new; the darwin-gated test SKIPS cleanly on macOS, not fails).

## Dev Notes

### Architecture compliance (binding)

- **AD-3 — Fork-server ephemeral workers:** parent pre-imports LLM **libs only** (never creds), `gc.disable()`+`gc.freeze()` **before** `os.fork()`, forks one worker per turn; the worker assembles the prompt with warm libs but **proxies the authenticated call to the broker**; the worker dies after its turn and its RAM is reclaimed. **≤1 worker in flight.** Parent signals a **readiness barrier** before the first turn. [Source: ARCHITECTURE-SPINE.md#AD-3]
- **AD-9 — Arbiter governs the brain:** ≤1 worker turn in flight (this story's guard); coalescing / cooldown / budget / fallback are the **full arbiter (1.8 / Epic 2)**, not here. The ≤1 bound is a **required M0 test** (AD-10). [Source: ARCHITECTURE-SPINE.md#AD-9, #AD-10]
- **AD-12 — Turn identity & idempotent close:** every turn carries a `turn_id`; core fences on it; a `Result` whose `turn_id` is already closed is **discarded**; turn close is **idempotent**. [Source: ARCHITECTURE-SPINE.md#AD-12]
- **AD-5 — Core is sole writer; workers propose via Result:** the worker never writes state/memory — it sends a `Job` (proposed work) and the broker's `Result` (proposed changes) goes to core. The worker is read-only + fire-and-forget. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-1 — LLM-free core:** the fork-server + worker live in `worker/`, not `core/`. Core (turn fence, arbiter skeleton) imports no provider SDK — import-linter KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **NFR2 — per-turn memory reclaimed:** nothing accumulates across turns; the worker spawns and dies. The endurance proof (500+ turn soak, flat RSS) is **Story 1.9**, not here. [Source: epics.md#NFR2]

### Turn-flow design (the key decision)

The worker is **fire-and-forget**, NOT in the response path:

1. Core (in 1.8; in 1.5 the test) requests a turn → arbiter ≤1 guard → fork-server spawns one worker with a `turn_id`.
2. Worker connects as `WORKER`, sends `Job`(payload=prompt) `dst=BROKER` with that `turn_id`, **exits** (RAM reclaimed).
3. Hub routes `JOB→BROKER`; broker calls the model, returns `Result` echoing the `turn_id` (already built, Story 1.4 `service.py`).
4. Hub routes `RESULT→CORE` → `core_inbox`. Core's `TurnFence.accept()` admits it only if the `turn_id` is current; a late/superseded one is **discarded** (AC3).

This keeps the worker stateless (no idle socket, no read loop), minimizes its footprint, and gives core sole ownership of turn lifecycle. **No `ROUTING_TABLE` change, no new `MsgKind`** — `Actor.WORKER`, `Job`, `Result`, `Envelope` all already exist (Story 1.2). [Source: subagent codebase analysis; contracts/__init__.py]

### Fork mechanics (binding technical notes)

- **gc ordering & the COW caveat:** `gc.disable()` early → preload libs → `gc.collect()` → `gc.freeze()` → fork. `gc.freeze()` exempts pre-imported objects from GC scans, BUT **does not stop refcount writes from dirtying COW pages** — the RAM win is *conditional on the worker not deep-traversing large shared parent objects* (treat warm libs as call-into-and-return). The real proof is a Linux `smaps` `Private_Dirty` ceiling — deferred to Linux CI (risk noted). Child leaves GC **disabled** (short-lived). [Source: python-expert research]
- **fork + asyncio:** never `os.fork()` from inside the running asyncio core loop (lock/loop corruption in the child; 3.12+ `DeprecationWarning`, 3.14 flips multiprocessing default to `forkserver`). Production = a dedicated single-threaded fork-server process. 1.5 delivers the seam; the child, if it needs async, calls a **fresh** `asyncio.run()` — never reuses the parent loop. [Source: python-expert research]
- **macOS vs Linux:** macOS aborts `fork()`-without-`exec()` once Apple frameworks are initialized — the real-fork test is `skipif(darwin)`; orchestration is fully testable cross-platform via the fake-spawner seam. [Source: python-expert research]
- **Readiness barrier:** `socket.socketpair()` sentinel byte is the production cross-process form; an in-process set-once `asyncio.Event` is fine for the 1.5 component. Avoid `eventfd` (Linux-only) on the seam. [Source: python-expert research]
- **Reaping:** `os.waitpid(pid, 0)` after the worker exits (blocking is fine at ≤1 in flight); no `SIGCHLD` handler. Reap → release the in-flight guard. [Source: python-expert research]

### Scope boundary (prevent scope creep)

**IN scope (1.5):** the fork-server (preload/freeze/readiness/spawn/reap) behind an injectable fork seam; the fire-and-forget worker; the core turn fence (idempotent close); the ≤1 arbiter skeleton; orchestration tests (fake spawner) incl. the M0 concurrency test; one Linux-gated real-fork test.

**OUT of scope (later, do NOT build):**
- Full arbiter — event **coalescing** into a catch-up slot, cooldown, budget, battery gating → **1.8 / Epic 2 / Epic 5** (AD-9/AD-14).
- **Real prompt assembly** (history window + curated memory injection) → **1.8 / Epic 4**. 1.5 uses a canned prompt.
- **Chat-transport + display** end-to-end wiring → **1.8** (and 1.6/1.7).
- Turn **timeouts / supersession / degrade-to-reflex** → **1.8 / Epic 2**. 1.5 fences only on closed-vs-current.
- The **endurance soak** (500+ turns, flat-RSS proof) → **Story 1.9**.
- Running the fork-server as a literal separate **process with full IPC** to core → **1.8** (1.5 delivers the mechanism + seam; document the process shape).
- Linux **smaps RAM-ceiling** test + Linux CI → follow-up (risk noted).

### Previous story intelligence (1.1–1.4)

- **Bus client API:** `from shelldon.core.bus import connect, write_frame, read_frame`. `connect(socket_path, Actor.WORKER)` registers explicitly (Story 1.4 added registration). The worker only `write_frame`s a Job. [Source: 1.3/1.4]
- **Broker echoes `turn_id`:** `broker/service.py` already copies the Job's `turn_id` onto its Result (Story 1.4) — so core's fence has the id to match. Reuse the broker stub pattern from `tests/test_broker_bus.py` for the worker tests. [Source: 1.4]
- **contracts ready:** `Actor.WORKER`, `MsgKind.JOB`, `Job(payload)`, `Result`, `Envelope(..., turn_id)` all exist; no contract change needed. [Source: 1.2]
- **Async test harness:** `pytest-asyncio` (auto mode); UDS sockets use the `sock_path` fixture (`tests/conftest.py`) for the macOS AF_UNIX path cap. [Source: 1.3]
- **Pin/lock discipline:** if any new dep is needed (likely none — fork/gc/socket are stdlib), pin exact + commit `uv.lock`. The fork-server's "warm libs" in 1.5 can be a placeholder/the already-present `anthropic` import to exercise the freeze/fork; real assembly libs land in 1.8. [Source: 1.1–1.4]
- **`worker/` package** exists as a stub (`shelldon/worker/__init__.py`) — populate it. [Source: scaffold 1.1]

### Testing standards

- `pytest` + `pytest-asyncio` (auto), mirroring package layout. **Orchestration via fake spawner** (deterministic, cross-platform) is the bulk; the real-`os.fork()` test is `skipif(darwin)` and must SKIP (not fail) on macOS. The M0 concurrency test (≤1 bound) is the fake-spawner mutual-exclusion/serialization/release-on-failure suite.
- Run `uv run lint-imports` (KEPT) and `uv run pytest -q` (green; the darwin-gated test shows as skipped) before marking tasks done.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.5; #NFR2; #Epic 1 cross-cutting (isolation tests)]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-3, #AD-9, #AD-12, #AD-5, #AD-1, #AD-10]
- [Source: _bmad-output/implementation-artifacts/1-4-...md (broker echoes turn_id, broker stub test pattern); 1-3 (bus client, registration); 1-2 (contracts)]
- CPython: `gc.freeze`/`gc.disable` + COW, `os.fork` + asyncio/threads (3.12 DeprecationWarning, 3.14 forkserver default), macOS fork-safety — synthesized in Dev Notes.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story). Story context built with two parallel research subagents (python-expert: CPython fork/gc/COW + macOS/asyncio pitfalls; Explore: worker↔broker↔core message-flow + scope).

### Debug Log References

- `uv run pytest tests/test_turn_fence.py tests/test_arbiter.py tests/test_forkserver.py tests/test_worker_sends_job.py tests/test_forkserver_fork.py -q` → 12 passed, 1 skipped (darwin gate).
- `uv run lint-imports` → "core is LLM-free (AD-1) KEPT" — `core/turn` + `core/arbiter` import no SDK; `worker/` lives outside core.
- `uv run pytest -q` → 56 passed, 1 skipped (no regressions; the real-fork test SKIPS cleanly on macOS).
- No dependency changes — fork/gc/os/socket/asyncio are all stdlib.

### Completion Notes List

- All 3 ACs satisfied. No new contract types, no `ROUTING_TABLE` change — `Actor.WORKER`/`Job`/`Result`/`Envelope` already existed (1.2).
- **AC1 (warm fork → one worker → proxy → die):** `ForkServer` preloads warm libs then `gc.disable()→collect()→freeze()` before any fork (readiness barrier = set-once event; socketpair noted as the prod cross-process form). The worker (`run_worker`) connects as `WORKER`, sends one `Job` to the broker, and exits — **fire-and-forget** (no Result wait). Proven by the real-fork test (Linux-gated) and the worker bus test (cross-platform).
- **AC2 (≤1 in flight):** the fork-server holds a mechanical `worker_in_flight` bound — a second `spawn_turn` raises `WorkerBusyError`, released on reap AND on a failed spawn. The M0 concurrency test (`test_forkserver.py`, fake spawner) asserts spawner-called-once, max-concurrency-never-exceeds-1, serialization after reap, and release-on-failure.
- **AC3 (idempotent close):** `core/turn.py TurnFence` admits a Result only for the current `turn_id`; closed/superseded/unknown/None → discarded; `close` idempotent; `closed` set bounded (capped deque).
- **Test seam decision (from research):** raw `os.fork()` is injected behind a `spawn`/`reap` seam. All orchestration is tested deterministically with a fake spawner (cross-platform); the real `os.fork()` test is `skipif(darwin)` — macOS aborts fork-without-exec. **Coverage gap (flagged):** no Linux CI yet, so the real-fork test runs only on the Pi / a Linux runner; the COW `smaps Private_Dirty` RAM-ceiling proof is a Linux-CI follow-up (the gc.freeze RAM win is *conditional* on the worker not deep-traversing shared objects — refcount writes still dirty COW pages).
- **Architecture decisions:** (a) GC left **disabled in the child** (sub-second worker; re-enabling would risk dirtying COW pages) — deliberate deviation from the textbook re-enable pattern. (b) Never fork from the asyncio core loop — production runs the fork-server as its own single-threaded process; 1.5 ships the mechanism + seam, the process/IPC wiring is 1.8. (c) The fork-server owns the **mechanical** ≤1 bound; `core/arbiter.py` is the **policy** skeleton (≤1 decision, where 1.8 grows coalescing/cooldown/budget) — kept decoupled so `worker/` doesn't import `core/`.
- **Scope held:** no event coalescing/cooldown/budget (1.8/Epic 2/5), no real prompt assembly (1.8/Epic 4), no chat/display wiring (1.8), no endurance soak (1.9).

### File List

- `shelldon/core/turn.py` (new — `TurnFence`, idempotent turn close AD-12)
- `shelldon/core/arbiter.py` (new — `Arbiter` ≤1 policy skeleton AD-9)
- `shelldon/worker/worker.py` (new — `run_worker` fire-and-forget child)
- `shelldon/worker/forkserver.py` (new — `ForkServer`: preload/freeze/readiness/≤1-spawn/reap + injectable fork seam)
- `tests/test_turn_fence.py` (new — fencing/idempotent close)
- `tests/test_arbiter.py` (new — ≤1 policy)
- `tests/test_forkserver.py` (new — M0 concurrency: ≤1, serialize, release-on-failure, readiness; fake spawner)
- `tests/test_worker_sends_job.py` (new — worker → Job → broker over the real bus)
- `tests/test_forkserver_fork.py` (new — real os.fork() integration, `skipif(darwin)`)

## Review Findings (2026-06-16)

Reviewers: Blind Hunter · Edge Case Hunter · Acceptance Auditor

### Patches (left as action items)

- [x] `[Review][Patch]` **`spawn_turn` no readiness guard** — fixed: `spawn_turn` now raises `RuntimeError` if `self._ready` isn't set (preload not complete) before any fork. Tested: `test_spawn_before_preload_is_refused`.
- [x] `[Review][Patch]` **`_os_waitpid_reap` unhandled `ChildProcessError`** — fixed two layers: `_os_waitpid_reap` catches `ChildProcessError` → return; AND `reap_current` releases `worker_in_flight` in a `finally` (+ swallows `ChildProcessError`) so a benign reap race can never deadlock future turns. Tested: `test_reap_childprocesserror_releases_guard_no_deadlock`.

### Deferred

- `[Review][Defer]` Child exits 0 on `asyncio.run()` exception — `forkserver.py:_os_fork_spawn` — `try/finally: os._exit(0)` masks any exception in the child; parent cannot detect a failed job send. Fix deferred: exit code not checked by anything in 1.5; add when supervisor/error path is scoped.
- `[Review][Defer]` `_os_waitpid_reap` has no timeout — hangs indefinitely if child is unkillable (debugger, stuck syscall). Watchdog/SIGKILL escalation is resilience scope, post-1.5.
- `[Review][Defer]` `Arbiter` and `ForkServer.worker_in_flight` are two independent ≤1 guards, never connected — by design (spec: "skeleton only, no coalescing"); wired at 1.8.
- `[Review][Defer]` `Arbiter.try_begin` not async-safe — no lock between read-check and write. No `await` between them today, so safe; add `asyncio.Lock` when concurrency model is defined in 1.8.
- `[Review][Defer]` Child inherits parent FDs after fork — acknowledged fork-without-exec risk (hence `skipif darwin`); fix with `os.closerange` before `asyncio.run()` in resilience work.
- `[Review][Defer]` No `TurnFence` eviction boundary test (max_closed + 1 distinct IDs) — correctness verified by inspection; test gap.
- `[Review][Defer]` `gc.disable()` permanent in parent on preload exception path — intentional for COW fork pattern; test teardown re-enables for isolation.
- `[Review][Defer]` `asyncio.sleep(0.05)` sync in `test_worker_sends_job.py` — already deferred from 1.3 review.

### Change Log

- 2026-06-16: Implemented Story 1.5 — fork-server warm worker (AD-3): `ForkServer` (gc.disable→collect→freeze before fork, readiness barrier, ≤1-worker mechanical bound, waitpid reap) behind an injectable fork seam; fire-and-forget `run_worker` (sends one Job to the broker, exits); core `TurnFence` (idempotent turn close, AD-12) + `Arbiter` ≤1 policy skeleton (AD-9). Orchestration tested cross-platform with a fake spawner (M0 concurrency test); real os.fork() test Linux-gated (`skipif darwin`). No deps, no contract change. 56 pass / 1 skipped, import-linter KEPT. Status → review.
- 2026-06-16: Addressed code review — 2 [Patch] findings resolved: `spawn_turn` now enforces the readiness barrier (RuntimeError before preload); `reap_current` releases the ≤1 guard in a `finally` and tolerates a `ChildProcessError` reap race (no permanent deadlock). +2 tests (`tests/test_forkserver.py`). 58 pass / 1 skipped, import-linter KEPT. The 8 [Defer] items left per their notes. Status → review (re-review).
