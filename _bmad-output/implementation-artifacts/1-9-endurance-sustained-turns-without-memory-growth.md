---
baseline_commit: 6f280d8e0c6212e11057d3575b2175d4331a6bf4
---

# Story 1.9: Endurance — sustained turns without memory growth

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want proof the pet survives a long run of turns without RAM creeping up,
so that v1's defining failure (OOM) is verifiably gone, not just designed against (AD-3, AD-9, AD-12, NFR2).

## Acceptance Criteria

1. **Flat memory under sustained load (NFR2):** Given the skeleton running (through Story 1.8), when it processes a long, sustained sequence of turns (e.g. 500+ over an extended soak), then resident memory stays flat within a defined bound — no monotonic growth — because workers spawn and die and nothing accumulates across turns.
2. **RAM reclaimed, ≤1 worker under load:** Given the soak run, when any worker turn completes, then that worker's memory is reclaimed and at no point does more than one worker live — the soak corroborates Story 1.5's ≤1 bound under sustained load.

## Tasks / Subtasks

> **This is a test/proof story — it adds almost no production code.** 1.1–1.8 built and wired the machine; 1.9 *proves it endures*. The work is two soak tests that reuse the Story 1.8 in-process harness verbatim, plus a stdlib-only memory probe and a registered `soak` marker. **Resist building anything new in `shelldon/`** — if a test surfaces a real leak, fixing *that* is in scope; pre-emptive "optimization" is not.
>
> **The honest split (read first — this shapes the whole story):** the 1.8 in-process harness runs the worker as an `asyncio.create_task(run_worker(...))` in the **same process** — so there is no separate process whose RSS gets reclaimed on exit. The in-process harness therefore **cannot** prove AC1's "workers spawn and die and RAM is reclaimed"; it can only prove the *complementary* half: **core/arbiter/fence/bus do not accumulate** across turns. The true NFR2 proof (parent RSS stays flat as real children fork, run, and `os._exit(0)`) requires the **real `os.fork()` path**, which is **Linux-gated** (skipped on macOS, runs on the Pi/Linux CI runner) exactly like `tests/test_forkserver_fork.py`. So 1.9 ships **two** tests, one per half. Do NOT try to measure RSS reclamation from the in-process harness — it will look flat for the wrong reason and prove nothing.
>
> **Provider is always a fake (no network).** Reuse `OkProvider` from the 1.8 harness. Each turn is tiny, so 500 turns run in seconds in-process.

- [x] **Task 1: Register the `soak` marker + a stdlib memory probe (no new deps)** (AC: 1)
  - [x] In `pyproject.toml` `[tool.pytest.ini_options]`, add a `markers` array registering `soak: long-running endurance/soak proof (NFR2)` so the new tests carry an explicit marker and pytest emits no unknown-marker warning. Keep `asyncio_mode = "auto"` and `testpaths` unchanged.
  - [x] Add a small stdlib memory probe **inside the new test module** (single-use → no shared util module): a `_rss_kb()` reader that parses Linux `/proc/self/statm` (the **second** field, 0-based index 1 = resident pages; multiply by `os.sysconf("SC_PAGE_SIZE")`) for **current** parent RSS — this is what must stay flat (NOT `resource.getrusage().ru_maxrss`, which is peak-only/monotonic and has platform-dependent units, so it can't show flatness). Use `tracemalloc` for the cross-platform in-process heap-growth bound. **No `psutil`** (not a dependency; stdlib only — AD discipline).

- [x] **Task 2: Cross-platform in-process "core does not accumulate" soak** (AC: 1, 2)
  - [x] New `tests/test_endurance_soak.py`. Reuse the 1.8 harness by importing its public helpers from `tests.test_end_to_end_turn` (`build_harness`, `Spawns`, `OkProvider`, `_await`) — no change to the committed 1.8 test file. (If churn is acceptable you *may* instead extract those into `tests/harness.py` and import from both, but the import-from-test-module path is the minimal-change default.)
  - [x] Drive `N = int(os.environ.get("SHELLDON_SOAK_TURNS", "500"))` turns **sequentially**: `source.feed(f"msg {i}")`, then `await _await(lambda: len(outbound) == i+1)` (one reply per turn — coalescing stays out of the way because each turn fully completes before the next is fed). Mark the test `@pytest.mark.soak`.
  - [x] Start a `tracemalloc` baseline after a warmup (~20 turns), then assert at the end:
    - **≤1 under sustained load (AC2 corroboration):** `spawns.count == N` and `spawns.max_live == 1`.
    - **No unbounded core state:** `core.arbiter._pending == []` and `core.arbiter.worker_in_flight is False`; `core.fence.current is None`; `len(core.fence._closed) <= 256` (the `TurnFence` closed-history deque is capped — this is the key "closed turn_ids don't grow forever" assertion); `len(core._bg) == 0` after a short drain (no leaked reap tasks); `core._seq == 2 * N` (exactly thinking+reply face per turn — monotonic, bounded, predictable).
    - **No heap growth (AC1, in-process half):** `tracemalloc` traced-memory delta from the post-warmup baseline to the end is within a defined bound (start at a few MB; tighten to the observed steady-state once it runs). Sample mid-run too and assert the late delta is not a monotonic climb.
  - [x] If any assertion fails because of a *real* leak (e.g. an unbounded set somewhere, or `_bg` not draining), **fix the leak in `shelldon/` as part of this story** — that is the whole point of the soak. Note the fix in the Dev Agent Record.

- [x] **Task 3: Linux-gated real-fork RSS-flatness soak (the true NFR2 proof)** (AC: 1, 2)
  - [x] In the same file, a second test gated with `pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="real fork is Linux-only; prod target is the Pi")` **plus** `@pytest.mark.soak`. Mirror `tests/test_forkserver_fork.py`'s gating and its GC restore.
  - [x] Compose a `Core` whose spawner is the **real** `ForkServer(sock_path)` (NO injected seam → default `_os_fork_spawn` + `_os_waitpid_reap`), with `run_broker` (`OkProvider`), `run_display` (`StubRenderer`), and `run_cli_transport` connected as in-process tasks — same wiring as `build_harness` but with the real fork-server. (Reuse `build_harness` if you parameterize the spawner; otherwise inline the wiring. Forking from the asyncio loop is acceptable **in a test** — it's how 1.5's real-fork test already runs; the "never fork from the loop in prod" rule is a deployment concern, AD-3, deferred.)
  - [x] Drive `N` real-fork turns sequentially (same feed→await-reply loop). Sample parent RSS via `_rss_kb()` after each turn (or every k turns). Assert:
    - **Flat within a bound (AC1):** discard a warmup prefix (~first 10–20 turns to reach steady state), then the **median RSS of the last quartile minus the median of the first quartile is below a defined bound** (start ~5–10 MB; tighten to observed). Also assert no monotonic climb (the per-quartile medians don't strictly increase across all four quartiles). Document the chosen bound + turn count in a comment with the rationale.
    - **≤1 + reclaimed (AC2):** the run completes with **no `WorkerBusyError`** (the real `ForkServer.worker_in_flight` mechanical guard would raise if two ever overlapped — a clean run proves ≤1 under load), and `fs.worker_in_flight is False` at the end (every child reaped, RAM reclaimed).
  - [x] `finally`: `await core.bus.stop()`, cancel tasks, and restore GC with `gc.unfreeze(); gc.enable()` (preload froze) — exactly as `test_forkserver_fork.py` does, so GC state doesn't leak to later tests on the Linux runner.

- [x] **Task 4: Verify guard + full suite** (AC: 1, 2)
  - [x] `uv run lint-imports` → contracts still KEPT (this story adds tests only; `core/` stays LLM-free).
  - [x] `uv run pytest -q` → green on macOS: the in-process soak runs, the real-fork soak is **skipped** (darwin), everything else stays green. Confirm the `soak` marker registers with no unknown-marker warning.
  - [x] Document in the Dev Agent Record: the exact turn count and memory bounds chosen (with observed steady-state numbers), and an explicit note that the real-fork RSS proof is **verified on Linux/Pi only** (skipped locally). State how to run the extended soak (`SHELLDON_SOAK_TURNS=5000 uv run pytest -m soak`).

## Dev Notes

### Architecture compliance (binding)

- **AD-3 — Fork-server ephemeral workers:** "a fork-server parent pre-imports LLM **libs only** … and `os.fork()`s one worker per turn; the worker … dies after its turn and **its RAM is reclaimed**. **At most one worker in flight** (see AD-9)." 1.9 is the *endurance proof* of this — sustained turns, flat RSS, every worker reaped. The COW/`gc.freeze()` mechanics are 1.5's; 1.9 proves the steady-state outcome under load. [Source: ARCHITECTURE-SPINE.md#AD-3]
- **AD-9 — The arbiter governs the brain:** "≤1 worker turn in flight … The ≤1-worker bound is a **required M0 test**." 1.5 tested ≤1 in isolation; 1.9 corroborates it **under sustained load** (`max_live == 1` across 500+ turns) and proves the coalescing/admission state (`_pending`, `worker_in_flight`) returns to rest each turn — no growing backlog. [Source: ARCHITECTURE-SPINE.md#AD-9]
- **AD-12 — Turn identity & idempotent close:** every turn carries a `turn_id`; core fences on it; closed turn_ids are tracked. The soak asserts the `TurnFence` closed-history is **bounded** (`deque(maxlen=256)`) — proving 500+ turns don't grow an unbounded closed-id set (a latent leak class). [Source: ARCHITECTURE-SPINE.md#AD-12]
- **AD-10 — Tests from M0:** "M0 tests **must** cover … the **≤1-worker-in-flight** bound (AD-9)." 1.9 extends that required bound from a single-shot check to a sustained-load check. [Source: ARCHITECTURE-SPINE.md#AD-10]
- **NFR2 / 512MB platform bound:** the prevented failure is "v1's OOM (RAM accumulating across turns)"; the platform is the "Raspberry Pi Zero 2W — 512MB". The doc has no separately-labelled "NFR2" block — the OOM-prevention contract lives in **AD-3's Prevents clause** and the Structural-Seed 512MB bound. 1.9's flat-RSS assertion is the direct test of it. [Source: ARCHITECTURE-SPINE.md#AD-3 (Prevents), #Structural-Seed]

### Why two tests (the load-bearing design decision)

The in-process harness (1.6/1.7/1.8) spawns the worker as `asyncio.create_task(run_worker(...))` in the **same process**. There is no child RSS to reclaim, so an in-process soak's RSS would look flat *regardless of whether the design works* — it would prove nothing about AC1's reclamation claim. Hence:

- **In-process soak (cross-platform, always runs):** proves the half that genuinely lives in-process — **core/arbiter/fence/bus do not accumulate** (bounded internal state + bounded Python heap) across 500+ turns, and ≤1 worker under load.
- **Real-fork soak (Linux-gated, runs on Pi/CI):** the **actual NFR2 proof** — real children fork, run, `os._exit(0)`, get `waitpid`-reaped, and the **parent's `/proc/self/statm` RSS stays flat**. This is the only place "RAM is reclaimed" is truly observable.

Splitting this way keeps the everyday `pytest` run fast and cross-platform while still landing the real proof on the target hardware. Be explicit in the completion notes that the RSS reclamation claim is verified on Linux only.

### Reuse verbatim (from Story 1.8 — `tests/test_end_to_end_turn.py`)

- `build_harness(sock_path, *, provider, spawns, turn_timeout=5.0)` → starts Core + broker + display + transport on one socket, waits for bus start + actor registration, returns a `Harness(core, tasks, source, outbound, renderer, spawns)`. `Harness.teardown()` cancels tasks + `await core.bus.stop()`.
- `Spawns(worker=run_worker)` → the in-process spawn seam with `.count` / `.live` / `.max_live` concurrency counters (the ≤1 assertion surface). Injected via `ForkServer(sock_path, spawn=spawns.spawn, reap=spawns.reap, manage_gc=False)`.
- `OkProvider` → fake provider returning `f"reply to: {prompt}"` (no network).
- `_Source` → controllable inbound (`.feed(line)` / `.close()`); `_await(predicate, timeout=2.0)` → 10ms poll until true or `AssertionError`.
- For the real-fork test, the **real** `ForkServer(sock_path)` (no seam) is the spawner; `Core(sock_path, fs, turn_timeout=...)` drives it. `Core._seq` increments twice per turn (thinking + reply face). [Source: 1.8 runtime.py, test_end_to_end_turn.py]

### Real-fork gating + GC restore (from Story 1.5 — `tests/test_forkserver_fork.py`)

```python
import sys, gc, pytest
pytestmark = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="fork-without-exec is unsafe on macOS frameworks; prod target is Linux",
)
# ... in finally: gc.unfreeze(); gc.enable()   # preload() froze; don't leak GC state
```

`ForkServer.preload()` runs `gc.disable()` → import → `gc.collect()` → `gc.freeze()` when `manage_gc=True` (the real-fork default). The in-process harness passes `manage_gc=False` (no fork → don't touch global GC). [Source: 1.5 forkserver.py preload(), test_forkserver_fork.py]

### Memory measurement (stdlib only — no new deps)

- **Current parent RSS (Linux, for flatness):** read `/proc/self/statm`, take field index 1 (resident pages), multiply by `os.sysconf("SC_PAGE_SIZE")`. This is the only stdlib source of *current* RSS that can show a flat-vs-climbing trend. (Linux-only — fine, the RSS test is Linux-gated anyway.)
- **In-process heap bound (cross-platform):** `tracemalloc.start()` → run → `tracemalloc.get_traced_memory()` → bound the delta. Catches a pure-Python accumulation even where RSS wouldn't move.
- **Avoid:** `resource.getrusage().ru_maxrss` (peak only, monotonic, KB-on-Linux/bytes-on-macOS unit split — can't show flatness) and `psutil` (not a dependency).

### Project Structure Notes

- New: `tests/test_endurance_soak.py`. Modified: `pyproject.toml` (register the `soak` marker only). **No `shelldon/` production change is expected** — unless the soak surfaces a real leak, in which case the minimal fix to the leaking module is in scope (and the soak is what proves the fix). `core/` stays LLM-free; import-linter must stay KEPT. [Source: ARCHITECTURE-SPINE.md#Structural-Seed, #AD-1]

### Scope boundary (prevent scope creep)

**IN scope (1.9):** the `soak` marker; the two soak tests (in-process core-no-accumulation + Linux-gated real-fork RSS flatness); a stdlib RSS/heap probe; fixing any *real* leak the soak exposes.

**OUT of scope (later, do NOT build):**
- **Real LLM / network in the soak** → always a fake provider. Live-provider endurance is not a unit/integration concern.
- **Overnight/scheduled soak automation, CI cron, Pi-in-the-loop runners** → deployment-hardening. 1.9 makes the soak *runnable* (and parameterizable via `SHELLDON_SOAK_TURNS`); it does not schedule it.
- **`psutil` or any memory-profiling dependency** → stdlib only.
- **Performance/latency benchmarking, throughput tuning** → not this story; 1.9 is about *memory flatness*, nothing else.
- **The "never fork from the asyncio loop" production process/IPC shape** → deferred (AD-3 / 1.8 note). The real-fork test forks from the loop, as 1.5's does.
- **Re-architecting the harness** → reuse 1.8's verbatim; extraction to `tests/harness.py` is optional, not required.

### Testing standards

- `pytest` + `pytest-asyncio` (auto mode), tests mirror package layout, bound every wait with a timeout (reuse `_await`) so a wiring miss fails fast rather than hanging. The soak tests carry `@pytest.mark.soak`. Default `SHELLDON_SOAK_TURNS=500` keeps the normal `pytest` run fast; an extended manual soak uses a larger value. Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green; real-fork soak skipped on macOS). [Source: 1.8 testing standards; ARCHITECTURE-SPINE.md#AD-10]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.9; #Epic 1 cross-cutting ("Story 1.9 then proves it endures")]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-3, #AD-9, #AD-12, #AD-10, #Structural-Seed]
- [Source: tests/test_end_to_end_turn.py (build_harness, Spawns, OkProvider, _Source, _await — reuse); shelldon/core/runtime.py (Core, _seq, arbiter/fence at rest); shelldon/core/turn.py (TurnFence closed-deque maxlen=256)]
- [Source: tests/test_forkserver_fork.py (real-fork Linux gating + gc.unfreeze/enable restore); shelldon/worker/forkserver.py (_os_fork_spawn, _os_waitpid_reap, preload gc.freeze, worker_in_flight)]
- [Source: _bmad-output/implementation-artifacts/1-8-…md (the in-process harness this story reuses); 1-5-…md (the ≤1 bound + real-fork path 1.9 corroborates under load)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (1M context)

### Debug Log References

- `uv run pytest -q` → 89 passed, 2 skipped (both Linux-gated real-fork tests: `test_forkserver_fork.py` + the new real-fork soak), 0 failed.
- `uv run lint-imports` → 2 contracts kept, 0 broken (tests-only story; `core/` untouched).
- `uv run pytest -m soak -q` → 1 passed, 1 skipped, 89 deselected — the `soak` marker selects exactly the two soak tests.
- Marker registration verified with `-W error::pytest.PytestUnknownMarkWarning` → no warning.
- In-process heap delta observed N-independent: **72,546 bytes @ 500 turns**, **73,768 bytes @ 2000 turns** (flat → no core accumulation; the ~73 KB is the saturated 256-entry `TurnFence` history + tracemalloc overhead).

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- **No production code changed** — 1.9 is a proof story. The only non-test change is registering the `soak` pytest marker in `pyproject.toml`. The soak surfaced **no leak**, so no `shelldon/` fix was needed.
- **Two complementary tests (the load-bearing design):** the in-process harness runs the worker as an asyncio task in the same process, so it CANNOT prove RAM reclamation — it proves the complementary half (core/arbiter/fence/bus don't accumulate) and ≤1 under load. The real-fork test (Linux-gated, skipped on macOS) is the true NFR2 RSS-flatness proof. **The RSS reclamation claim is verified on Linux/Pi only — skipped locally on darwin.**
- **N-independent heap measurement:** test-side O(N) retention (the `outbound` list and the display stub's `rendered` list) is cleared each turn during measurement, so the heap delta reflects only what core/bus/runtime retain. Verified flat at both 500 and 2000 turns; without this, a fixed bound would falsely pass small and falsely fail large. Bound set to 250 KB (~3.4× observed).
- **Determinism via full-rest gating:** each turn waits for BOTH the arbiter policy guard AND the ForkServer mechanical guard to release (`_at_rest`) before feeding the next message — otherwise messages coalesce and break the one-turn-per-message accounting (would also hit the P1 `WorkerBusyError` drop path). This yields exact assertions: `spawns.count == N`, `max_live == 1`, `core._seq == 2*N`, `delivered == N`, bounded `fence._closed <= 256`, drained `_bg`.
- **Real-fork RSS bound** (`RSS_FLAT_BOUND_KB = 10 MiB`, last-quartile vs first-quartile median delta after warmup) is set generously on first landing since it can't be observed on macOS — **tighten after the first real Pi/Linux run**. ≤1-under-load is proven by a clean run with no `WorkerBusyError` (the mechanical guard would have raised).
- **Extended soak:** `SHELLDON_SOAK_TURNS=5000 uv run pytest -m soak` (default 500 keeps the normal suite fast). No new dependency (stdlib `tracemalloc`/`os`/`gc`; `/proc/self/statm` for RSS — no `psutil`).

### File List

- `pyproject.toml` (modified — register the `soak` pytest marker)
- `tests/test_endurance_soak.py` (new — in-process core-no-accumulation soak + Linux-gated real-fork RSS-flatness soak)

## Code Review Findings

All findings are in `tests/test_endurance_soak.py`. No production code changes required.

### Must Fix

- [x] **GC state leak if `bus.stop()` raises (line 214)** — In `test_real_fork_rss_stays_flat`, the `finally` block calls `core.bus.stop()` and then `gc.unfreeze(); gc.enable()` sequentially. If `bus.stop()` raises, the GC restore calls are skipped, leaving GC frozen for all subsequent tests on the Linux CI runner. Fix: wrap `bus.stop()` in its own `try/finally` so the GC calls always execute.

- [x] **Missing per-quartile monotonic-climb check (line 200–206)** — The flatness assertion only compares first-quartile vs last-quartile median. A stair-step leak (e.g. Q1=50, Q2=3500, Q3=7000, Q4=120 KiB) passes because `delta = last_q - first_q` is small, even though RSS climbed monotonically mid-run. Add a check that the four-quartile medians are not in strict ascending order: `medians = [_median(rss[k*q:(k+1)*q]) for k in range(4)]` then `assert medians != sorted(medians)`.

- [x] **`_median([])` crashes with IndexError when `rss` is empty (line 200)** — If `SHELLDON_SOAK_TURNS=0`, the for loop never runs, `rss = []`, and `_median(rss[:q])` raises a bare `IndexError`. Add `assert rss, f"no RSS samples collected (SOAK_TURNS={SOAK_TURNS}, warmup={warmup})"` before the quartile block. Separately: replace the hand-rolled `_median` with `from statistics import median as _median` (stdlib handles empty input with a clear `StatisticsError`).

### Should Fix

- [x] **`max(samples)` catches early spikes but misses slow growth (line 121)** — `tracemalloc.get_traced_memory()[0]` returns current live bytes since `start()`, so `max(samples)` is the peak across all samples — it fires on a one-time early allocation that later frees (false positive) and passes a slow 300-byte/turn accumulation that stays under the 250 KB ceiling (false negative). Replace with `heap_delta = samples[-1] - samples[0]`: the first sample is near-zero right after `start()`, the last reflects steady state, and the difference grows with N if there is a real per-turn leak.

- [x] **`_Source` and `_await` imported by private name from sibling test (line 37)** — These underscore-prefixed symbols are imported cross-file. A rename in `test_end_to_end_turn.py` breaks this file at pytest collection time with a generic `ImportError` and no attribution. Add a comment: `# _Source, _await: private helpers from 1.8 harness — update here if renamed there`.

### Resolution (2026-06-17)

All 7 findings addressed in `tests/test_endurance_soak.py` (tests-only; no production change). Suite 89 passed / 2 skipped; in-process heap delta 65,858 B @ 500 / 66,992 B @ 2000 turns (N-independent, < 250 KB bound); contracts KEPT.

- **Must #1 (GC leak on `bus.stop()` raise):** wrapped `await core.bus.stop()` in its own `try/finally` so `gc.unfreeze(); gc.enable()` always run.
- **Must #2 (mid-run climb missed) — implemented robustly, NOT as suggested:** the suggested `assert medians != sorted(medians)` **false-fails on genuinely flat RSS** (all four quartile medians equal → `sorted == medians` → fails), and a strict-ascending check flakes ~4% on noise. Instead: switched the flatness metric to the four-quartile **spread** (`max - min`, subsumes first-vs-last and catches any spike/climb) under `RSS_FLAT_BOUND_KB`, plus a non-flaky monotonic guard — fail only if RSS climbs across **all** quartiles **and** the spread exceeds `RSS_CLIMB_FLOOR_KB` (1 MiB). Flat/equal data and single transient spikes don't trip it.
- **Must #3 (`_median([])` crash / empty rss):** replaced the hand-rolled median with `statistics.median`; added `assert rss` and `assert len(rss) >= 4` guards before the quartile block.
- **Should #4 (`max(samples)` misses slow growth):** now `heap_delta = samples[-1] - samples[0]` with an explicit baseline captured right after `tracemalloc.start()` — won't fire on a one-time early alloc that frees, and grows with N on a real per-turn leak.
- **Should #5 (private cross-file import):** added a NOTE comment by the `from test_end_to_end_turn import ...` line flagging that `_Source`/`_await` are private 1.8 helpers to update on rename.
- **Nice #6 (deque/set comment):** corrected to "eviction logic in TurnFence.close() keeps _closed (a set) bounded ≤256".
- **Nice #7 (`_bg` internal poll):** kept (it proves background reap tasks don't accumulate — a real leak class) with a comment explaining why `_at_rest` alone is insufficient (done-callback discard can lag a tick).

### Nice to Have

- [x] **Comment on line 134 says "deque cap" but checks the set** — `h.core.fence._closed` is a `set[str]`, not the deque. The eviction logic in `TurnFence.close()` keeps the set bounded to ≤256, so the assertion passes correctly, but the comment is wrong. Change to: `# eviction logic in TurnFence.close() keeps _closed bounded`.

- [x] **`len(h.core._bg) == 0` polls an internal implementation detail (line 135)** — If `Core` ever switches from a bare `set` to `asyncio.TaskGroup` or a counter, this silently breaks (likely hangs until timeout). The `_at_rest` check at the end of each `one_turn` already guarantees the reap task has completed. Consider dropping this assertion or documenting why `_at_rest` is insufficient here.

## Change Log

- 2026-06-16 — Story 1.9 implemented: endurance/soak proof of NFR2. Added the `soak` marker and `tests/test_endurance_soak.py` — a cross-platform in-process soak (≤1 worker under load, bounded core state, N-independent flat heap over 500+ turns) and a Linux-gated real-fork RSS-flatness soak (the true RAM-reclamation proof, skipped on macOS). No leak found → no production change. Suite 89 passed / 2 skipped; import contracts kept. Status → review.
- 2026-06-16 — Code review complete. 3 must-fix, 2 should-fix, 2 nice-to-have findings added above. Status remains review pending fixes.
- 2026-06-17 — Addressed all 7 review findings (tests-only). Must-fix #2 implemented as a robust quartile-spread + non-flaky monotonic-climb guard rather than the suggested strict-ascending check (which false-fails on flat RSS). Suite 89 passed / 2 skipped; heap delta N-independent (~66 KB); contracts KEPT. Status → review.
