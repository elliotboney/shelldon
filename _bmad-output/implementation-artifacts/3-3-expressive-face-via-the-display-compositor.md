---
baseline_commit: 4127783bbdd58493e9dbdb839132d4bcd869b969
---
# Story 3.3: Expressive face via the display compositor

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet's face to reflect its drifting mood — chosen from a **starter emotion set I can edit** (and that the bot will later extend itself),
so that I can read how my pet feels at a glance, and its expressions aren't frozen in code (CAP-2, AD-5, AD-7).

## Acceptance Criteria

1. **A self-modifiable faces registry, seeded with the starter set, corruption-tolerant:** Given core starts, when it loads the faces registry, then expressions are defined in an **editable data file** (`~/.shelldon/faces.toml`) — **not a hardcoded enum** — **seeded with the six starter emotions (content, sleepy, curious, grumpy, excited, low-battery)** on first run, each entry carrying its **name, the mood region that selects it (valence/arousal/energy ranges), and a render token**. An absent file is seeded from built-in defaults; a corrupt/invalid file **falls back to the built-in defaults and logs a warning, never crashing** (the AD-7/3.1 corruption-tolerance discipline). The path is **injectable** (tests never touch real `$HOME`).
2. **Core maps mood→face and pushes the token; the display renders it latest-wins:** Given the personality-state drifts (the Story 3.2 reflex tick), when **no LLM turn is active**, then **core** selects the matching face from the registry (a pure `select_face(mood) → token`) and **pushes a face snapshot (monotonic `seq`)**; the display renders it **latest-wins** (its existing compositor — unchanged). Core is the sole owner/writer of the faces registry (AD-5). **Turn lifecycle faces (`thinking`/`reply`/`cant-think`) still own the screen while a turn is in flight** — the reflex tick does **not** push a mood face during a turn (it still mutates state, per 3.2 AC2).
3. **Core can apply a validated face addition atomically (the "core applies" half of self-modify):** Given a request to add/replace a face, when `core.apply_add_face(...)` runs, then core **validates** it against a closed schema (non-empty unique name; well-formed in-range mood ranges) — **rejecting an invalid one without mutating the registry** — and on success **atomically writes `faces.toml` (temp + rename, preserving the file's human comments) and updates the in-RAM registry**. Each of the **six starter expressions renders a visibly distinct token** when selected (real E-Ink sprites are the hardware renderer, deferred). The **chat-driven path** (the LLM proposing `add_face` over a `Result`) is **Story 3.4** — 3.3 builds and tests only the in-core apply path it will call.

> **Scope seam (binding):** 3.3 builds the **self-modifiable faces substrate + the core mapping/apply half** — the editable `faces.toml` registry (seed + load + corruption-tolerance), a pure `select_face(mood) → token`, core pushing the mood face between turns, and `core.apply_add_face()` (validate + atomic comment-preserving write + in-RAM update). It does **NOT** build: the **chat-driven proposal protocol** — the LLM emitting a structured `add_face` and `Result` carrying proposed memory-ops — that is **Story 3.4** (it front-runs Epic 4's AD-6 memory-op machinery and will *call* the `apply_add_face` built here); the **real E-Ink expression bitmaps / partial-refresh sprite rendering** (the production `Renderer` — hardware-gated, like Story 1.7 deferred it); any **new bus contract / `Result` field** (the wire is unchanged — `add_face` proposals over the bus are 3.4); the **battery signal** that should drive `low-battery` (PiSugar2 is Epic 5 — `low-battery` is selectable by low `energy` for now). The single biggest mistake here is building 3.4's LLM→`Result` proposal protocol, or changing `contracts/`.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent):**
> - **The display is already the compositor AC1 describes.** `display/service.py` keys snapshots by `Region` (a closed enum in `contracts/`), applies **latest-wins by monotonic `seq`**, and coalesces bursts under E-Ink's slow refresh; core already owns the `face` region. **AC1's "compositor of regions with a closed/registered region-id, core owns face" shipped in Story 1.7 — 3.3 changes the display very little (it renders whatever face token arrives).** [Source: shelldon/display/service.py:30-79, shelldon/contracts/__init__.py:39-47]
> - **`StateSnapshot.face` is a free string token** and core pushes it via `Core._push_face(face)` (`runtime.py`). The lifecycle tokens `FACE_THINKING="thinking"`, `FACE_REPLY="happy"`, `FACE_DEGRADED="cant-think"` already exist. **3.3 pushes the mood-derived token through this same `_push_face` — no contract change, no display change.** [Source: shelldon/core/runtime.py:43-51,199-208]
> - **Story 3.2 left the mood→face wiring to 3.3 on purpose.** `Core._reflex_tick` mutates mood/energy via `apply_patch` but deliberately does NOT push a face. **3.3 adds the push** — gated on "no turn in flight" so lifecycle faces aren't clobbered (use the existing `self.fence.is_idle` / `self.arbiter.is_idle` predicates). [Source: shelldon/core/runtime.py:_reflex_tick, shelldon/core/turn.py, shelldon/core/arbiter.py:22-29]
> - **Story 3.1's atomic-write recipe is the template for writing `faces.toml`:** temp file in the same dir → `flush` → `os.fsync` → `os.replace`, parent dir created, never a half-written file (AD-10). Reuse that exact pattern for the registry write. [Source: shelldon/core/state.py:checkpoint]
> - **Story 3.1's corruption-tolerant load is the template for loading `faces.toml`:** absent → defaults; corrupt/invalid → warn + built-in defaults, never raise. Reuse that philosophy. [Source: shelldon/core/state.py:load]
> - **Mood lives in `PersonalityState` (3.1):** `mood.valence`, `mood.arousal`, `energy` — the inputs to `select_face`. The closed writable paths are unchanged; 3.3 adds no new state field. [Source: shelldon/core/state.py]
> - **No TOML, no faces file, and no worker→core "proposed change" path exist yet.** 3.3 introduces the first TOML in the tree. stdlib `tomllib` READS TOML (Python 3.13); **writing** TOML needs a dependency (chosen: `tomlkit`, which preserves the human comments/ordering on a programmatic rewrite — see Task 1). The worker-proposes/core-applies wire path is **Story 3.4**, not here.

- [x] **Task 1: Add the `tomlkit` dependency (TOML read+write, comment-preserving)** (AC: 1, 3)
  - [x] Add `tomlkit` to `pyproject.toml` `dependencies` and lock it (`uv add tomlkit`; pin the resolved version like the existing pins, e.g. `tomlkit==0.13.x`). Rationale: stdlib `tomllib` is **read-only**; the bot+human co-edit `faces.toml`, so the programmatic write must **preserve comments/ordering** — `tomlkit` round-trips them (`tomli-w` would clobber them). `tomlkit` is pure-Python and tiny.
  - [x] Confirm `tomlkit` is allowed in `core/`: it is NOT an LLM/provider lib, so the AD-1 import-linter forbidden list (`openai`/`anthropic`/…) is unaffected. (Pre-flight: run `uv run lint-imports` after adding — must stay KEPT.)

- [x] **Task 2: The faces registry — schema, built-in starter set, load + corruption-tolerance** (AC: 1)
  - [x] Create `shelldon/core/faces.py`. Define a **face entry** shape (name, `valence`/`arousal`/`energy` selection ranges as `[lo, hi]`, and a render `token` — default the token to the name). Define `DEFAULT_FACES` — the **six starter emotions** with sane, **non-overlapping-enough** mood ranges plus a broad catch-all so selection always resolves: suggested ordering/criteria (first match wins): `low-battery` (energy ≤ ~0.15), `sleepy` (low arousal + low-mid energy), `grumpy` (valence < 0), `excited` (high valence + high arousal + high energy), `curious` (mid-positive valence + elevated arousal), `content` (broad positive/neutral fallback). Tune to taste — exact thresholds are not load-bearing as long as all six are reachable and there's always a match.
  - [x] `FaceRegistry.load(path)`: absent file → **seed**: write `DEFAULT_FACES` to `faces.toml` (atomic, with a header comment telling the owner they can edit it) and use them; present + valid → load; present but **corrupt/invalid (bad TOML, missing fields, malformed ranges)** → **built-in `DEFAULT_FACES` + `log.warning`, never raise** (AC1). Default path `~/.shelldon/faces.toml` as `DEFAULT_FACES_PATH`, **injectable** for tests.
  - [x] Validate every loaded entry against the same closed schema `apply_add_face` uses (Task 4) — a malformed entry triggers the whole-file fallback (fail safe, like 3.1's decode tolerance).

- [x] **Task 3: Pure `select_face(registry, valence, arousal, energy) → token`** (AC: 2)
  - [x] A **pure function** (no I/O, no mutation): return the `token` of the first registry entry whose ranges all contain the given mood/energy; if none match, return a **defined default** (the catch-all/`content`). Deterministic for a given `(registry, mood)` — unit-testable directly. This is the mood→face mapping the epic places in "core/display"; per the owner decision it lives in **core** (which owns the self-modifiable registry — AD-5 sole writer of soul data).

- [x] **Task 4: `core.apply_add_face(...)` — the validated, atomic "core applies" half** (AC: 3)
  - [x] In `faces.py` (`FaceRegistry.add_face`) and exposed via `Core.apply_add_face(...)`: **validate** a proposed face against a closed schema — non-empty `name`; ranges are `[lo, hi]` with `lo ≤ hi` and within bounds (valence/arousal ∈ [-1, 1], energy ∈ [0, 1]); reject a duplicate name unless an explicit replace flag. On any validation failure **raise/return an error and do NOT mutate the registry or the file** (fail fast — the 3.1 whole-patch-reject discipline).
  - [x] On success: update the **in-RAM** registry AND **atomically** write `faces.toml` using `tomlkit` (read-modify-write preserving comments) + the 3.1 temp→fsync→`os.replace` recipe. The in-RAM and on-disk views stay consistent.
  - [x] **In-core + synchronous (AD-5):** `apply_add_face` is the single-writer apply path. Do NOT add any bus/worker call here — Story 3.4 wires the LLM proposal to *call* this method. Keep it callable standalone.

- [x] **Task 5: Wire core to push the mood face between turns** (AC: 2)
  - [x] In `Core`, construct the `FaceRegistry` on startup (injectable path, like `PersistentState`). After `_reflex_tick` applies a mood patch, **if no turn is in flight** (`self.fence.is_idle` and/or `self.arbiter.is_idle`), resolve `select_face(...)` from current mood/energy and `await self._push_face(token)`. Skip the push when a turn is active so the lifecycle face (`thinking`/`reply`/`cant-think`) is not clobbered (the reflex still mutates state — 3.2 AC2).
  - [x] Only push when the resolved token **changes** (avoid spamming identical face snapshots / burning `seq`); reuse the "skip the no-op" instinct from 3.2's empty-patch handling.
  - [x] Do not alter the existing lifecycle pushes (`_start_turn`/`_handle_result`/`_degrade`) beyond what's needed; keep the turn path behavior intact.

- [x] **Task 6: Tests** (AC: 1, 2, 3)
  - [x] **AC1:** load with no file → built-in defaults AND a seeded `faces.toml` written (assert the six are present); load a valid hand-written file → those faces; **corrupt/invalid file → built-in defaults + no raise** (garbage TOML, missing field, bad range). Injected `tmp_path` faces file — never real `$HOME`.
  - [x] **AC2 (selection):** `select_face` returns the expected token for representative moods (each of the six reachable), a boundary case, and the default fallback; assert it's **pure** (no mutation of the registry). **AC2 (push gating):** after a reflex tick with the fence idle, core pushes the mood token; with a turn in flight, core does **not** push a mood face (lifecycle face stands); an unchanged token does not re-push.
  - [x] **AC3 (apply):** `apply_add_face` with a valid new face → in registry + present in the re-read `faces.toml` + a pre-existing **comment survives** the write (proves `tomlkit` preservation); invalid (empty name, `lo > hi`, out-of-range, duplicate without replace) → rejected, **registry and file unchanged**; the write is atomic (a simulated failure before `os.replace` leaves the prior file intact — mirror 3.1's crash test). **AC3 (distinct):** each of the six starter expressions resolves to a distinct token.
  - [x] Add a `tmp_path`/monkeypatch isolation so `DEFAULT_FACES_PATH` never resolves to real `$HOME` in any test (extend the existing autouse `_isolate_state_checkpoint` conftest fixture to also redirect the faces path).

- [x] **Task 7: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (`core/` gains `faces.py` + `tomlkit`; no LLM/provider import enters core; AD-1 holds).
  - [x] `uv run pytest -q` → green (existing 174 unchanged + the new faces tests). Default run hits no network and writes no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **CAP-2 — Aliveness / expressive face:** "Delivers the expressive face as a first-class deliverable, not the skeleton's placeholder." 3.3 turns 3.2's drifting mood into a chosen, owner-editable expression. [Source: epics.md#Epic 3]
- **AD-5 — Core is the sole writer; the soul is core-owned:** the faces registry is **core-owned soul data** (same principle as the personality-state and the curated memory tree). Core is the sole writer; `apply_add_face` is the single-writer apply path. The display "never reads shared memory — it renders what arrives" (a pushed token). **Sanctioned deviation:** the epic AC says "the *display* maps mood/energy"; per the owner decision (self-modifiable faces), the mapping lives in **core** (which owns the editable registry), and the display stays a dumb token renderer. This better serves the self-modify goal and AD-5's dumb-display principle. [Source: ARCHITECTURE-SPINE.md#AD-5; owner decision 2026-06-17]
- **AD-7 / AD-10 — editable file, corruption-tolerant, atomic write:** `faces.toml` is the editable data layer; load tolerates corruption (defaults + warn, never crash) and writes are atomic (temp + fsync + rename) — the same M0 crash-safety invariant 3.1 introduced. [Source: ARCHITECTURE-SPINE.md#AD-7,#AD-10]
- **AD-1 — LLM-free core:** 3.3 adds `core/faces.py` + the `tomlkit` lib; neither is an LLM/provider lib, so the import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **AD-11 — closed/registered types:** `Region` (the display region id) stays the closed contract type. The **face vocabulary is intentionally an open, data-driven registry** (the epic calls it a "**starter** set"), validated against a closed *schema* (name + ranges + token) rather than a closed *enum* — so the owner/bot can extend it without a code change while a typo'd entry is still rejected by the schema. [Source: ARCHITECTURE-SPINE.md#AD-11; epics.md#Story 3.3]
- **NFR3 — E-Ink slow refresh:** unchanged — the display's existing latest-wins + coalescing already absorbs reflex-driven face churn; the real partial-refresh/layered-sprite rendering is the hardware `Renderer` (deferred). [Source: epics.md#NFR3, shelldon/display/service.py]

### Design guidance (what to build, minimally)

- **Faces are data, not an enum.** The owner's v1 win was telling the pet to add new faces; a hardcoded enum forecloses that. So the vocabulary is a `faces.toml` registry seeded from built-in `DEFAULT_FACES`. Built-ins guarantee the system works with no/corrupt file; the file makes it editable today (by the owner) and extensible later (by the bot, Story 3.4).
- **Core maps; pushes a token; display unchanged.** `select_face` is a pure function in core; core pushes the resolved token through the existing `_push_face`. The display already renders face-token snapshots latest-wins — do not modify it (beyond confirming behavior). This keeps the wire contract and the display untouched.
- **Gate the mood push on "no turn in flight."** Reflexes always mutate state (3.2), but the mood face should only reach the screen between turns; during a turn the lifecycle face owns the screen. Use `fence.is_idle`/`arbiter.is_idle`. Push only on a token change.
- **`tomlkit` for comment-preserving writes.** `faces.toml` is co-edited by human (comments, tuning) and core (programmatic adds). `tomllib` (stdlib) reads; `tomlkit` writes while preserving comments/ordering. Reuse the 3.1 atomic temp→fsync→`os.replace` wrapper around the `tomlkit` dump.
- **Validation is the closed schema, shared by load and apply.** One validation function: non-empty name, ranges `[lo,hi]` with `lo≤hi`, valence/arousal ∈ [-1,1], energy ∈ [0,1]. Load uses it to fall back on a bad file; `apply_add_face` uses it to reject a bad proposal. Same fail-fast/whole-reject discipline as 3.1's `apply_patch`.
- **`low-battery` is energy-driven for now.** The real PiSugar2 battery signal is Epic 5; until then `low-battery` is selectable via low `energy`. Don't wire battery here.

### What 3.3 does NOT do

- **No chat-driven self-modify** — the LLM proposing `add_face`, `Result` carrying proposed memory-ops, the worker/core proposal protocol — that is **Story 3.4** (it front-runs Epic 4's AD-6 machinery and *calls* the `apply_add_face` built here). 3.3 builds and tests only the in-core apply path.
- **No new bus contract / `Result` field / `Envelope` kind** — `contracts/` is unchanged. Face tokens ride the existing `StateSnapshot.face` string; mood is read from core's own `PersonalityState`, not sent on the wire.
- **No real E-Ink expression bitmaps / partial-refresh sprite engine** — the production `Renderer` is hardware-gated (Story 1.7 deferred it). 3.3's "distinct face" is a distinct token; the recognizable bitmap comes with the panel.
- **No closed `Expression` enum in `contracts/`** — deliberately replaced by the data-driven registry (owner decision). `Region` stays the closed type.
- **No display changes beyond confirming the token render** — the compositor/latest-wins/closed-region-id (AC1) already shipped in 1.7.
- **No battery integration, no scheduler, no new state fields/paths.**

### Project Structure Notes

- **New:** `shelldon/core/faces.py` (face-entry schema + validation, `DEFAULT_FACES` starter set, `FaceRegistry` load/select/add_face/atomic-write, `DEFAULT_FACES_PATH`). New tests `tests/test_faces.py`.
- **Modified:** `shelldon/core/runtime.py` — construct the `FaceRegistry` on startup (injectable path); after `_reflex_tick`, push the mood token when idle + changed; add `Core.apply_add_face(...)` delegating to the registry. `pyproject.toml` — add+pin `tomlkit`. `tests/conftest.py` — extend the autouse isolation fixture to redirect `DEFAULT_FACES_PATH` to `tmp_path`.
- `core/` only (+ the `tomlkit` lib) → import-linter KEPT. Structural Seed lists `core/ … state/ … ` as domain; `faces.py` is the face-vocabulary sibling of `state.py`/`reflexes.py`. [Source: ARCHITECTURE-SPINE.md#Structural-Seed]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Reuse the shared `tests/conftest.py` helpers (`await_true`, `DummySpawner`) and the `tmp_path`-isolation pattern — **never write real `$HOME`** (extend the autouse fixture for the faces path too). `select_face` and the validation are **pure** — test them directly with fixed inputs (deterministic, no sleeps). For the atomic write, mirror 3.1's "crash before `os.replace` leaves the prior file intact" test. For comment-preservation, write a face and assert a seed comment survives the re-read. Prefer state-predicate assertions over sleep anchors (Epic 2 retro #1). Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: epic-2-retro-2026-06-17.md#action-items]

### Previous story intelligence (Stories 3.1 & 3.2 — just completed)

- **3.1 gives you the atomic-write + corruption-tolerant-load recipe verbatim** (`state.py::checkpoint`/`load`): temp→fsync→`os.replace`; absent→defaults, corrupt→defaults+warn-never-raise. Copy this shape for `faces.toml`. The 3.1 review added a non-positive-interval guard, loop-error survival, and "never write real `$HOME`" — the last is enforced by the autouse conftest fixture you must extend. [Source: shelldon/core/state.py, tests/conftest.py]
- **3.2 gives you the push-gating context.** `_reflex_tick` mutates mood but does not push a face (left for 3.3). Push only when idle + changed. The single-writer property (synchronous `apply_patch`, one core loop) means `apply_add_face` is also safe without a lock. [Source: shelldon/core/runtime.py:_reflex_tick]
- **Recurring review themes to pre-empt (from 3.1/3.2 reviews):** guard inputs (validate ranges/names; reject non-positive/out-of-range); don't silently swallow — `log.warning` on a fallback; no WHAT-comments (explain WHY only); keep long-lived tasks out of `_bg`; share test helpers via `conftest.py`, don't copy-paste. Building these in from the start avoids a repeat review cycle. [Source: 3-1 and 3-2 Review Findings]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 3 / Story 3.3 (this story); #Story 3.1 (state substrate — done); #Story 3.2 (reflex drift — done); #NFR3]
- [Source: ARCHITECTURE-SPINE.md#AD-5 (core sole writer, soul data, dumb display), #AD-7 (RAM + editable file), #AD-10 (atomic write), #AD-11 (closed region-id; open face registry w/ closed schema), #AD-1 (LLM-free core), #Structural-Seed]
- [Source: shelldon/display/service.py (the compositor: region-keyed, latest-wins by seq, coalescing — already satisfies AC1); shelldon/display/renderer.py (the injectable `Renderer` seam; real bitmaps deferred)]
- [Source: shelldon/core/runtime.py:43-51,199-208 (`_push_face` + lifecycle tokens), :_reflex_tick (3.2 left the mood push to 3.3), arbiter.is_idle/fence.is_idle (the push gate)]
- [Source: shelldon/core/state.py (`PersonalityState` mood/energy inputs; `checkpoint`/`load` atomic+tolerant recipe to copy)]
- [Source: shelldon/contracts/__init__.py:39-47 (`Region` closed enum — stays closed), :92-106 (`StateSnapshot.face` string token — unchanged, carries the mood token)]
- [Source: tests/conftest.py (`await_true`, `DummySpawner`, `_isolate_state_checkpoint` autouse fixture to extend for the faces path)]
- [Source: owner decision 2026-06-17 — self-modifiable data-driven faces (TOML), core-owns/maps, full chat-driven self-modify split to Story 3.4]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- `uv run pytest -q` → 196 passed, 2 skipped, 3 deselected (174 prior + 22 new faces/runtime tests).
- `uv run lint-imports` → both contracts KEPT (`tomlkit`/`tomllib`/`msgspec` in `core/faces.py` are not LLM/provider libs).
- `~/.shelldon` confirmed never created by a run (autouse fixture redirects both `DEFAULT_CHECKPOINT_PATH` and `DEFAULT_FACES_PATH`).
- Caught during verify: the new mood-face push fires inside the reflex loop, which would perturb the turn-counting `_seq` invariant of the 1.8 e2e / 1.9 soak harnesses if a tick landed mid-test. Parked `reflex_interval=3600` in `build_harness` and the real-fork soak so it provably never fires there (deterministic, not timing-dependent).

### Completion Notes List

- **AC1** — faces are an editable `~/.shelldon/faces.toml` registry (`core/faces.py`), **not an enum**: absent → seeded with the six starter emotions (each = name + valence/arousal/energy ranges + token); corrupt/invalid → built-in `DEFAULT_FACES` + a logged warning, never a crash (garbage TOML, inverted range, out-of-range all covered). Path injectable.
- **AC2** — `select_face` is a pure, deterministic mood→token mapping (first matching face wins; `content` is the broad catch-all so it always resolves). `Core._maybe_push_mood_face` runs after each reflex tick and pushes the token **only when the fence + arbiter are idle** (a turn's lifecycle face owns the screen otherwise — the reflex still mutated state per 3.2) and **only when the token changed**. `_push_face` now tracks `_last_face` for both lifecycle and mood pushes, so the mood face correctly restores after a `thinking`/`reply` face.
- **AC3** — `FaceRegistry.add_face` / `Core.apply_add_face` validate against the closed schema (non-empty name; in-range, well-ordered ranges; no duplicate unless `replace=True`) and reject without mutating RAM or disk; on success they update RAM and **atomically** rewrite `faces.toml` via `tomlkit` (read-modify-write) so the owner's header comment survives, using the 3.1 temp→fsync→`os.replace` recipe (crash-before-rename leaves the prior file intact — tested). The six starter tokens are distinct.
- **Decisions honored (owner, 2026-06-17):** faces = self-modifiable data (not an enum); **core** owns the registry and maps mood→face, pushing a token (display stays a dumb renderer — sanctioned deviation from the epic's "display maps"); **TOML + `tomlkit`** so hand comments survive bot writes; the **chat-driven proposal is split to Story 3.4**, which will call the `apply_add_face` built here.
- **Display unchanged** — AC1's compositor/latest-wins/closed-`Region` already shipped in 1.7; 3.3 pushes the mood token through the existing `_push_face`/`StateSnapshot.face`, no `contracts/` or display change.
- **Scope held:** no LLM→`Result` proposal protocol (3.4), no `contracts/` change, no real E-Ink bitmaps (hardware renderer deferred), no closed `Expression` enum, `low-battery` driven by low `energy` (PiSugar2 is Epic 5), no new state fields.

### File List

- `shelldon/core/faces.py` (new) — `Face` struct, `DEFAULT_FACES`/`STARTER_NAMES`, pure `select_face`, `FaceRegistry` (load+seed+corruption-tolerance / select / validated atomic comment-preserving `add_face`), `DEFAULT_FACES_PATH`, atomic-write helper.
- `shelldon/core/runtime.py` (modified) — construct `FaceRegistry` on startup (injectable `faces_path`); `_maybe_push_mood_face` (idle-gated, change-gated) called after each reflex tick; `_push_face` tracks `_last_face`; `Core.apply_add_face(...)`.
- `pyproject.toml` (modified) — add+pin `tomlkit==0.15.0` (core-only, comment-preserving TOML writes); `uv.lock` updated.
- `tests/test_faces.py` (new) — 22 tests across AC1/AC2/AC3 + runtime mood-push gating + `apply_add_face`.
- `tests/conftest.py` (modified) — autouse fixture also redirects `DEFAULT_FACES_PATH` to `tmp_path`.
- `tests/test_end_to_end_turn.py`, `tests/test_endurance_soak.py` (modified) — park `reflex_interval=3600` so the mood push can't perturb their `_seq` counting.

### Review Findings

- [x] [Review][Important] `tests/test_faces.py` fallback test used a truthiness assert (`assert reg.select(...)`) that would pass on `""` — RESOLVED: changed to `== "content"` (the defined `DEFAULT_FACE_TOKEN`).
- [x] [Review][Minor] `FaceRegistry._write` took unused `replaced`/`face` params — RESOLVED: dropped from the signature and call site (it rebuilds the AOT from the full list).
- [x] [Review][Minor] `_validate_face` `isinstance(name, str)` redundant — RESOLVED: simplified to `if not face.name:` (verified `not face.name` already catches `""`/`None`; a truthy non-str is an out-of-contract caller).

## Change Log

| Date       | Change                                                                 |
|------------|------------------------------------------------------------------------|
| 2026-06-17 | Implemented Story 3.3: self-modifiable faces substrate (editable `faces.toml` seeded with the 6 starter emotions, corruption-tolerant), pure mood→face `select_face`, core pushes the mood token between turns (idle+change gated), and `apply_add_face` (validated, atomic, comment-preserving via `tomlkit`). Owner decisions: faces=data, core-maps, chat-driven split to 3.4. 196 tests green; contracts KEPT; no real `$HOME` writes. |
| 2026-06-17 | Addressed code review: 1 important (truthiness assert → value check) + 2 minor (dead `_write` params, redundant `isinstance`) resolved. 196 green; contracts KEPT. |
