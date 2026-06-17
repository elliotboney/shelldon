---
baseline_commit: f63780813d4505bc0e6e06f01de185ce5b355962
---
# Story 3.1: Persistent personality-state struct

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to have an inner state (mood, energy, last-interaction) that survives restarts,
so that it has continuity of self — it isn't reborn blank every reboot (AD-7, AD-5, CAP-2).

## Acceptance Criteria

1. **Loads on start, defaults cleanly on first run:** Given core, when it starts, then a personality-state struct (mood / energy / last-interaction) lives in RAM and is **restored from its last checkpoint**, **defaulting cleanly on first run** (no checkpoint file yet → sane defaults, no crash).
2. **Core sole writer; sparse dotted-path patches; periodic checkpoint (not per-change):** Given the struct changes, when writes occur, then **core is the sole writer**, mutations are **sparse patches over fixed dotted paths** (e.g. `mood.valence`) validated against a **closed set** of allowed paths (an unknown path is rejected, not silently created), and state is **checkpointed periodically to one small file — NOT on every change** (SD-wear, NFR7).
3. **Survives abrupt power loss between checkpoints:** Given an abrupt power loss between checkpoints, when the pet restarts, then it **restores the last checkpoint without a corrupt-state crash** — a partially-written or corrupt checkpoint falls back to defaults (or the last good state) rather than crashing; worst case it loses only the changes since the last checkpoint.

> **Scope seam (binding):** 3.1 builds the **state substrate only** — the struct, a sparse-patch writer over fixed dotted paths, a periodic **atomic** checkpoint, and restore-with-corruption-tolerance. It does **NOT** build: the **reflex loop** that drives mood/energy drift (blink/idle/time-of-day) — that is **Story 3.2**, which will *call* this struct's patch API on a tick; the **mood→face expression mapping** — **Story 3.3**; the **scheduler** that will later own the checkpoint cadence — **Epic 5 (Story 5.1)**; **injecting state into the prompt/turn** — **Epic 4 (Story 4.4)**. See Dev Notes "What 3.1 does NOT do." The single biggest mistake here is building 3.2's drift logic or 3.3's expressions inside 3.1.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent):**
> - `core/runtime.py::Core` — the single-consumer loop (`run()` over `bus.core_inbox`). Core is accessed serially (no lock). It already owns the bus/fence/arbiter and has a background-task pattern: `_track(task)` adds to `self._bg` and `_cleanup()` cancels them on teardown. **The periodic checkpoint flush is a background task in this same pattern.** [Source: shelldon/core/runtime.py:67-89, 191-204]
> - `msgspec` (pinned `0.21.1`) is the project's serialization lib. The wire contracts in `contracts/` use `msgspec.Struct, frozen=True`. **Personality state is mutable RAM state, so it is NOT frozen** — a different use than the wire structs. [Source: shelldon/contracts/__init__.py]
> - The `Region` enum (`contracts/__init__.py`) is the precedent for a **closed/registered set that rejects typos** (AD-5/AD-11). The closed set of allowed dotted state-paths follows the same principle. [Source: shelldon/contracts/__init__.py:39-47]
> - **Nothing persistent-state, atomic-write, or `~/.shelldon`-state-file related exists yet** — 3.1 introduces the first atomic file write and the first state checkpoint in the codebase. There is no composition root (`app.py`) yet either; Core is constructed by tests today. Keep the path **injectable** so tests never touch real `$HOME`.

- [x] **Task 1: Define the personality-state struct + a closed set of writable dotted paths** (AC: 1, 2)
  - [x] Create `shelldon/core/state.py`. Define a **mutable** msgspec `Struct` (NOT `frozen`) — minimal shape: `mood` (a small sub-struct with at least `valence: float`; `arousal: float` optional), `energy: float`, `last_interaction: str | None` (ISO-8601 UTC, `None` until the first interaction). Keep fields **minimal** — exact richer fields are deferred; do not model a full affect system.
  - [x] Define a **closed set** of writable dotted paths (e.g. `{"mood.valence", "mood.arousal", "energy", "last_interaction"}`) — the AD-5 "fixed dotted paths." A patch targeting a path outside the set is rejected (raises / returns an error), never silently sets a new attribute (typo-rejection, like the `Region` enum).
  - [x] Provide sane **defaults** (neutral mood, mid energy, `last_interaction=None`) so first-run construction needs no file.

- [x] **Task 2: Sparse-patch writer over fixed dotted paths** (AC: 2)
  - [x] In `state.py`, a `PersistentState` (or similar) wrapper owns the struct and exposes `apply_patch(patch: dict[str, <value>])` that sets each `dotted.path` → value, validating every key against the closed set first (reject the whole patch on an unknown path — fail fast, don't half-apply). Setting a value **marks the state dirty** (a `_dirty` flag) — this is the signal the periodic checkpoint uses so it can skip a no-op write.
  - [x] **Core is the sole writer (AD-5/NFR11):** the patch API is called only from the core loop. Do NOT add a bus message or worker path that writes state in 3.1 (reflexes in 3.2 call it in-process; worker-proposed state deltas are a later concern). Keep the API in-process and synchronous.

- [x] **Task 3: Atomic checkpoint write (temp + rename) — the first atomic write in the tree** (AC: 2, 3)
  - [x] `PersistentState.checkpoint(path)` serializes the struct (recommend `msgspec.json` for a small, human-readable file) and writes **atomically**: write to a temp file in the **same directory** as the target, `flush()` + `os.fsync()` the temp fd, then `os.replace(tmp, path)` (atomic rename on the same filesystem). Create the parent dir if missing. Clear `_dirty` only after a successful replace.
  - [x] This satisfies the AD-10 M0-required **atomic-write crash-safety** invariant: a write interrupted before the `os.replace` leaves the prior checkpoint (or no file) intact — never a half-written file the loader sees. Make the write seam testable (e.g. a test can simulate a crash between temp-write and replace and assert the prior file survives).

- [x] **Task 4: Restore on start + corruption tolerance** (AC: 1, 3)
  - [x] `PersistentState.load(path)` (or a classmethod / factory): if the file is **absent** → defaults (first run, AC1). If present and valid → restore. If present but **corrupt / partially written / schema-mismatched** → fall back to defaults (or last-good) and **log a warning**, never raise (AC3 "without a corrupt-state crash"). Catch decode/validation errors specifically; don't blanket-swallow.
  - [x] Construct/restore this in `Core.__init__` (or `Core.run()` startup) so the struct lives in RAM for the process lifetime, restored from the injected checkpoint path. Default the path to `~/.shelldon/state.json` (one small file, outside source — Structural Seed) but **accept an injected path** for tests.

- [x] **Task 5: Periodic checkpoint flush in core — NOT per change** (AC: 2)
  - [x] Add a minimal periodic flush in `Core`: a background asyncio task (tracked via the existing `_track`/`_bg` pattern) that, on an interval (`checkpoint_interval`, injectable; small in tests, e.g. seconds), calls `checkpoint()` **only if dirty**. This is the "periodic, not per-change" mechanism (AC2). Cancel it cleanly in `_cleanup()` (teardown must not hang or write a partial file).
  - [x] **Seam note (binding):** structure this flush so the Epic 3.2 reflex tick and the Epic 5 scheduler can later **subsume** it without changing checkpoint behavior — same pattern as 2.3's degrade and the 3.2 AC's "subsumable tick." Do NOT build a scheduler or a reflex tick here; a single in-core interval task is the minimal correct mechanism. (Optional, low-cost: also flush-if-dirty on graceful shutdown in `_cleanup` — only if it doesn't risk a partial write during cancellation.)

- [x] **Task 6: Tests** (AC: 1, 2, 3)
  - [x] **AC1:** load with no file → defaults, no crash; write a valid checkpoint, reload a fresh `PersistentState` from it → restored values match.
  - [x] **AC2:** `apply_patch({"mood.valence": …})` updates RAM + sets dirty; an unknown path (e.g. `"mood.nope"` or `"hp"`) is rejected and applies nothing; a single `apply_patch` does **not** write to disk (assert the file is unchanged / absent until a checkpoint runs) — proving "not on every change."
  - [x] **AC3 (the AD-10 invariant):** simulate a crash mid-write — e.g. leave a stray temp file or a truncated/garbage target file — and assert `load()` returns defaults (or last-good) without raising; and that an interrupted `checkpoint` (failure before `os.replace`) leaves the previous good file intact.
  - [x] Drive the periodic flush deterministically (small interval or call the flush directly) and assert: dirty → one write happens; not dirty → no write. Use an injected `tmp_path`-based checkpoint file; **never write real `$HOME`**.

- [x] **Task 7: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (3.1 is pure `core/` + `tests/`, no LLM/provider import enters core; AD-1 trivially holds).
  - [x] `uv run pytest -q` → green (existing 140 unchanged + the new state tests). Default run hits no network and writes no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **AD-7 — Volatile state lives in RAM, checkpointed:** "the personality-state struct … live in RAM, checkpointed periodically to one small file … RAM state itself is never the source of truth." This is the spine rule 3.1 implements directly: RAM is the working copy; the one small file is the restart safety-net, written periodically (not per change). [Source: ARCHITECTURE-SPINE.md#AD-7]
- **AD-5 — Core is the sole writer of state:** "only `core` mutates the personality-state struct … the state delta is a sparse patch over fixed dotted paths (e.g. `mood.valence`)." The patch API is in-core only; the closed set of dotted paths is the "fixed dotted paths." No worker/bus write path in 3.1. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-10 — atomic-write crash-safety is an M0-required test:** "a write interrupted mid-`rename` leaves the prior tree intact." 3.1 introduces the **first** atomic write in the codebase (temp + fsync + `os.replace`); Task 3/6 prove the crash-safety invariant. [Source: ARCHITECTURE-SPINE.md#AD-10]
- **NFR7 — SD-write-wear discipline:** "high-frequency state in RAM (periodic checkpoint)." The dirty-flag + interval flush is what keeps high-churn reflex writes (coming in 3.2) off the SD card. [Source: epics.md#NFR7]
- **NFR11 — core is the sole writer of all state:** patch API stays inside the core loop (serial access, no lock — same property the arbiter/fence rely on). [Source: epics.md#NFR11]
- **AD-1 — LLM-free core:** 3.1 adds only `core/state.py` + tests; no provider import enters `core/`. Import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **NFR1 — 512MB ceiling:** the struct is tiny and the checkpoint is one small file; nothing here accumulates. [Source: epics.md#NFR1]

### Design guidance (what to build, minimally)

- **Mutable struct, not frozen.** The wire contracts in `contracts/` are `frozen=True` because they're immutable messages. Personality state is mutated in place by core, so it's a **mutable** `msgspec.Struct` (or a small nested struct). Do not put it in `contracts/` — it's core-internal RAM state, not a bus message. Keep it in `core/state.py`.
- **Closed dotted-path set = typo rejection.** Mirror the `Region` enum precedent: a fixed set of writable paths so a typo (`mood.valnce`) is a rejected patch, not a silently-created attribute. Validate the whole patch before applying any of it (fail fast, no half-apply).
- **Serialization:** `msgspec.json` gives a small, human-readable, debuggable state file and round-trips a `Struct` cleanly — preferred over msgpack here (the file is tiny; readability helps debugging on the Pi). Decode failures (corrupt/garbage) raise `msgspec.DecodeError`/`ValidationError` — catch those specifically for AC3's corruption tolerance.
- **Atomic write recipe:** temp file in the **same dir** as the target (so `os.replace` is a same-filesystem atomic rename) → `f.flush()` → `os.fsync(f.fileno())` → `os.replace(tmp, target)`. Clear `_dirty` only after `os.replace` returns.
- **Periodic flush without a scheduler:** there is no scheduler (Epic 5) or reflex tick (3.2) yet, so 3.1 needs its own minimal periodic driver — a single in-core asyncio interval task (tracked in `self._bg`) that flushes if dirty. Keep it dead simple and clearly marked as the seam 3.2/5.1 will subsume (precedent: 2.3's degrade ack is the standalone version of the Epic-3 reflex; the 3.2 AC explicitly says the tick must be "subsumable later without changing behavior").
- **Injectable path + interval.** `Core(... , checkpoint_path=None, checkpoint_interval=…)` or a `PersistentState` constructed with both injected. Tests pass a `tmp_path` file and a tiny interval. Default path `~/.shelldon/state.json`. **No test may write real `$HOME`.**

### What 3.1 does NOT do

- **No reflex loop / mood drift / blink / idle / time-of-day** — that is **Story 3.2**. 3.1 only provides the struct + patch API; 3.2 is what *calls* `apply_patch` on a tick. Building drift logic here is out of scope.
- **No mood→face expression mapping, no real faces** — **Story 3.3** (`FACE_*` tokens stay placeholders; the starter emotion set content/sleepy/curious/grumpy/excited/low-battery is 3.3).
- **No scheduler** — **Epic 5 (5.1)**. The periodic flush is a minimal interim task, not a scheduler.
- **No state-into-prompt / memory-shapes-the-turn** — **Epic 4 (4.4)**. 3.1 doesn't touch the turn or the worker.
- **No worker/bus write path to state** — AD-5 says workers only *propose* via `Result`; that machinery is later (Epic 4/6). 3.1's writer is in-core only.
- **No new bus contract / Envelope kind** — `contracts/` is unchanged. The display still gets `FACE_*` via the existing `StateSnapshot`; wiring personality-state → face snapshots is 3.2/3.3.

### Project Structure Notes

- **New:** `shelldon/core/state.py` (the struct + closed dotted-path set + `PersistentState` with `apply_patch`/`checkpoint`/`load`). New tests `tests/test_state.py` (or similar).
- **Modified:** `shelldon/core/runtime.py` — construct/restore `PersistentState` on startup; add the periodic flush background task (existing `_track`/`_bg`/`_cleanup` pattern); accept injected `checkpoint_path`/`checkpoint_interval`. Keep changes surgical — don't refactor the turn/arbiter/fence logic.
- `core/` only → import-linter KEPT. Structural Seed lists `core/ … state/` — a single `state.py` is the minimal form (promote to a package later if it grows). [Source: ARCHITECTURE-SPINE.md#Structural-Seed]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Use a `tmp_path`-based checkpoint file for every test — never real `$HOME`. Assert on RAM values, dirty/clean transitions, file presence/contents, and the corruption-tolerance path. Drive the periodic flush with a tiny injected interval or by calling the flush directly (prefer state-predicate assertions over `sleep` anchors — Epic 2 retro action #1). Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: epic-2-retro-2026-06-17.md#action-items]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 3 / Story 3.1 (this story); #Story 3.2 (reflex loop — calls this struct, later); #Story 3.3 (expressions — later); #NFR7, #NFR11, #NFR1]
- [Source: ARCHITECTURE-SPINE.md#AD-7 (RAM + periodic checkpoint), #AD-5 (core sole writer, sparse dotted-path patches), #AD-10 (atomic-write crash-safety M0 test), #AD-1 (LLM-free core), #Structural-Seed (`core/state/`, state file outside source)]
- [Source: shelldon/core/runtime.py:67-89,90-111,191-204 (single-consumer loop + `_track`/`_bg`/`_cleanup` background-task pattern to reuse for the periodic flush)]
- [Source: shelldon/contracts/__init__.py:39-47 (`Region` enum — the closed-set/typo-rejection precedent for the fixed dotted paths); :92-106 (`StateSnapshot` — unchanged here; personality-state→face is 3.2/3.3)]
- [Source: _bmad-output/implementation-artifacts/epic-2-retro-2026-06-17.md (action #1 — prefer state-predicate asserts over sleep anchors)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- Full suite green after wiring the flush task: `uv run pytest -q` → 156 passed, 2 skipped, 3 deselected.
- `uv run lint-imports` → both contracts KEPT (core stays LLM-free; AD-1 trivially holds — 3.1 is pure `core/` + `tests/`).

### Completion Notes List

- **AC1** — `PersistentState.load(path)` returns clean defaults when the file is absent (no write, no crash) and restores values from a valid checkpoint; `Core.__init__` restores into RAM for the process lifetime from an injectable path.
- **AC2** — `apply_patch` validates every key against the closed `WRITABLE_PATHS` set BEFORE applying any (whole-patch reject on an unknown path; no half-apply), mutates RAM only, and sets `_dirty`. A single patch never writes to disk — the in-core `_checkpoint_loop` flushes periodically and only when dirty (proven by `test_patch_does_not_write_to_disk` + `test_core_flush_writes_only_when_dirty`). Core is the sole, in-process synchronous writer (no bus/worker write path added).
- **AC3** — checkpoint is the first atomic write in the tree (mkstemp in the same dir → write → `flush` → `os.fsync` → `os.replace`); a crash before the rename leaves the prior good file byte-for-byte intact, cleans up the temp, and keeps `_dirty` set. `load` tolerates garbage / truncated / schema-mismatched files by logging a warning and falling back to defaults, never raising.
- **Deviation from Task 5 wording (binding seam preserved):** the periodic flush is a long-lived SINGLETON task kept in its own `self._checkpoint_task` slot (cancelled in `_cleanup`), NOT in `self._bg`. `_bg` holds transient per-turn reap tasks whose "drains to 0" invariant the 1.9 soak asserts — a permanent resident there breaks that test. Cancellation/teardown still follows the existing pattern. Also added an optional best-effort flush-if-dirty on graceful shutdown in `_cleanup` (atomic write → no partial-write risk).
- **Scope held:** no reflex/drift loop (3.2), no mood→face mapping (3.3), no scheduler (5.1), no state-into-prompt (4.4), no new bus contract — `contracts/` unchanged.

### File List

- `shelldon/core/state.py` (new) — `Mood`/`PersonalityState` mutable structs, `WRITABLE_PATHS` closed set, `PersistentState` (`apply_patch`/`checkpoint`/`load`, `_dirty`), `DEFAULT_CHECKPOINT_PATH`.
- `shelldon/core/runtime.py` (modified) — restore `PersistentState` on construction; injectable `checkpoint_path`/`checkpoint_interval`; `_checkpoint_loop`/`_checkpoint_if_dirty`; cancel + flush-if-dirty in `_cleanup`.
- `tests/test_state.py` (new) — 16 tests across AC1/AC2/AC3 + the Core periodic flush.

### Review Findings

- [x] [Review][Patch] `_checkpoint_loop` silently dies on first disk error — add `except Exception as exc: log.warning(...)` + `continue` so one disk error doesn't permanently kill periodic checkpointing [`shelldon/core/runtime.py:_checkpoint_loop`] — RESOLVED: per-iteration `try/except Exception` logs + continues; state stays dirty so the next interval retries (`test_checkpoint_loop_survives_a_disk_error`).
- [x] [Review][Patch] `load()` missing `OSError` in except clause — `path.read_bytes()` can raise `PermissionError`/`FileNotFoundError` (TOCTOU after `exists()`) which propagates uncaught and crashes `Core.__init__` [`shelldon/core/state.py:84`] — RESOLVED: `OSError` added to the except; unreadable/TOCTOU-deleted checkpoint falls back to defaults (`test_load_unreadable_file_falls_back_to_defaults`).
- [x] [Review][Patch] `checkpoint_interval <= 0` not validated — zero or negative flows into `asyncio.sleep()`, spinning the event loop and starving all coroutines [`shelldon/core/runtime.py:__init__`] — RESOLVED: `Core.__init__` raises `ValueError` on a non-positive interval (`test_nonpositive_checkpoint_interval_rejected`).
- [x] [Review][Defer] `_checkpoint_task.cancel()` not awaited before shutdown flush — `_cleanup()` is sync so await is structurally constrained; low-severity "Task destroyed but pending" warning risk [`shelldon/core/runtime.py:_cleanup`] — deferred, structural constraint
- [x] [Review][Defer] Type mismatch in `apply_patch` (nan/inf/wrong-type values silently accepted) — value type/range not validated; mitigated once loop-recovery patch is applied [`shelldon/core/state.py:apply_patch`] — deferred, out of 3.1 scope
- [x] [Review][Defer] Mutable struct allows direct attribute bypass of `apply_patch` — inherent mutable design tradeoff; enforced by convention, 3.2+ calls `apply_patch` on a tick [`shelldon/core/state.py:Mood`] — deferred, by design
- [x] [Review][Defer] `checkpoint_path` is a public mutable attribute on `Core` — caller could change path mid-run; hygiene concern [`shelldon/core/runtime.py:__init__`] — deferred, pre-existing

## Change Log

| Date       | Change                                                                 |
|------------|------------------------------------------------------------------------|
| 2026-06-17 | Implemented Story 3.1: persistent personality-state substrate (struct + closed-set sparse-patch writer + atomic periodic checkpoint + corruption-tolerant restore). All ACs met; 156 tests green; import contracts KEPT. |
| 2026-06-17 | Addressed code review: 3 patch findings resolved (flush-loop survives disk errors, `load()` tolerates `OSError`/TOCTOU, non-positive `checkpoint_interval` rejected). +3 tests; 159 green; contracts KEPT. |
