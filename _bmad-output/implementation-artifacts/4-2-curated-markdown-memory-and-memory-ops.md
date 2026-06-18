---
baseline_commit: 4bdfaa5b83d0bc6e533bc665be8fcd9cae8e71e3
---

# Story 4.2: Curated markdown memory and memory-ops

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to keep a human-readable, LLM-curated record of what matters — and to read my authoritative `DIRECTIVE.md` first,
so that durable knowledge about me persists and shapes how it behaves (CAP-6, AD-5, AD-6).

## Acceptance Criteria

1. **A closed memory-op contract + a core-only apply path that writes the markdown tree atomically:** Given the memory-ops contract in `contracts/` (`remember`, `rewrite_about`, `log_episode`) with **fixed arg schemas** (a closed, validated set — typo/unknown op rejected), when `core.apply_memory_op(op)` runs, then core **validates** it and **writes the markdown tree** (`about.md`, `facts/`, `people/`, `episodes.md`) **atomically** (temp file + `os.replace`, the Story 3.1 idiom) — rejecting an invalid op without writing. **Core is the sole writer** (AD-5); the path is **injectable** (tests never touch real `$HOME`).
2. **`about.md` is bot-owned (core sole writer); `rewrite_about` persists a new curated doc:** Given `about.md`, when a `rewrite_about` op is applied, then the new curated doc replaces it atomically and persists for later reads. The owner does **not** hand-edit `about.md` (it is bot-owned). *(Injecting it into prompts is **Story 4.4** — 4.2 persists + exposes a read accessor, nothing assembles a prompt yet.)*
3. **`DIRECTIVE.md` is owner-only — read as authoritative, NEVER on core's write path (disjoint writers):** Given a human-only `DIRECTIVE.md` (the owner's "constitution"), when any turn/dream reads memory, then the bot reads it as **authoritative** (a read accessor exists, returns its content or None) and **NEVER writes it** — it is **not a memory-op target** and not reachable by `apply_memory_op` (disjoint writers: core owns `about.md`/`facts/`/`people/`; the owner owns `DIRECTIVE.md` — no conflict). A test proves no memory-op can write `DIRECTIVE.md`.
4. **`people/` records people the owner mentions:** Given `people/`, when a `remember`-style op targets a person (people the owner mentions — NOT BLE-detected), then that person is recorded as a file under `people/`. (BLE presence is Epic 7; 4.2 only records owner-mentioned people via a memory-op.)

> **Scope seam (binding):** 4.2 builds the **curated-memory substrate + the "core applies" half** — the closed memory-op schemas in `contracts/`, the `about.md`/`facts/`/`people/`/`episodes.md` markdown tree written atomically by `core.apply_memory_op`, and the read-only `DIRECTIVE.md` accessor with disjoint-writer enforcement. It does **NOT** build: the **worker-proposes-over-the-wire half** — the LLM emitting ops and `Result` carrying `proposed_ops`, which needs the **turn-topology reshape** (owner decision 2026-06-17: **worker emits the Result** — broker returns the completion to the worker, the worker parses → `Result.proposed_ops` → core). That reshape is a **separate follow-up story** that **3.4 (faces self-modify) also rides** — both call the validated apply paths built in 3.3/4.2; **injecting memory into the prompt** — **Story 4.4** (4.2 persists + exposes read accessors; nothing assembles a prompt yet); the **vault / uid isolation** — **Story 4.3**; the **`learnings` table / dream cycle** — AD-6 learnings + Epic 6. The single biggest mistake here is building the worker→Result wire or 4.4's prompt injection inside 4.2.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent):**
> - **The Story 3.1 atomic-write idiom is the markdown-write recipe verbatim:** temp file in the same dir → `flush` → `os.fsync` → `os.replace`, parent dir created, never a half-written file (AD-10). `core/state.py` and `core/faces.py` both use it; 4.2's markdown writes do the same. [Source: shelldon/core/state.py:checkpoint, shelldon/core/faces.py:_atomic_write_text]
> - **Story 3.3 `apply_add_face` is the exact shape of `apply_memory_op`:** validate a structured op against a closed schema → reject-without-writing on invalid → atomically persist on success → core is the sole, synchronous writer. Mirror it. The follow-up wire will call `apply_memory_op` just as 3.4 will call `apply_add_face`. [Source: shelldon/core/faces.py:add_face, shelldon/core/runtime.py:apply_add_face]
> - **Story 4.1 conventions for a core-owned store under `~/.shelldon/`:** injectable default path, create the parent dir, and **extend the autouse conftest fixture to redirect the default off real `$HOME` in the same change** (Epic 3 retro action #3 — caught twice before). [Source: shelldon/core/history.py, tests/conftest.py:_isolate_state_checkpoint]
> - **`contracts/` is the home for the memory-op schemas (AD-6):** closed, versioned msgspec structs, like `Job`/`Result`/`StateSnapshot`. Define `remember`/`rewrite_about`/`log_episode` as a closed tagged union here now (they are the shared vocabulary the follow-up wire + core both use); 4.2 does NOT yet attach them to `Result` (that's the wire follow-up). [Source: shelldon/contracts/__init__.py]
> - **Core is LLM-free (AD-1)** — the markdown layer is pure file I/O in `core/`; no provider import. Import-linter stays KEPT. [Source: pyproject.toml#tool.importlinter]

- [x] **Task 1: Closed memory-op schemas in `contracts/`** (AC: 1, 4)
  - [x] In `contracts/`, define the memory-op vocabulary as **closed, frozen msgspec structs** with **fixed arg schemas** (forbid unknown fields), tagged so the future wire can carry them as a union: `Remember` (record a fact/person — e.g. `collection: Literal["facts","people"]`, `name: str`, `content: str`), `RewriteAbout` (`content: str`), `LogEpisode` (`content: str`, optional `tags`). A `MemoryOp` union of the three. Keep fields minimal; the closed schema is the "fixed arg schemas, no free-text deltas" of AD-6.
  - [x] Do NOT attach `MemoryOp` to `Result`/`Envelope` yet — that is the wire follow-up. These types are defined now so core and the future worker share one vocabulary.

- [x] **Task 2: The curated markdown tree + atomic writer** (AC: 1, 2, 4)
  - [x] Create `shelldon/core/memory.py` (the curated markdown layer; the sqlite history layer stays `core/history.py`). Default root `~/.shelldon/memory/` (injectable). Lay out `about.md`, `facts/`, `people/`. Reuse the 3.1 atomic-write helper (temp + fsync + `os.replace`, create parent dir).
  - [x] `apply_memory_op(op: MemoryOp)`: dispatch on the op type — `RewriteAbout` → atomically replace `about.md`; `Remember(collection, name, content)` → atomically write `facts/<slug>.md` or `people/<slug>.md` (closed `collection` set; slugify `name` safely — no path traversal); `LogEpisode` → append/atomically write an episodes record. **Validate first, write only on success** (reject unknown/invalid op without touching disk — the 3.1/3.3 whole-reject discipline). Core is the sole, synchronous writer.
  - [x] **Path safety:** a `name` must not escape the tree (reject `../`, absolute paths, separators) — sanitize to a path-safe filename (Unicode-preserving; same-name overwrites by design — owner decision 2026-06-17). A memory-op must never write outside `about.md`/`facts/`/`people/`/`episodes.md`.

- [x] **Task 3: `DIRECTIVE.md` read accessor + disjoint-writer guarantee** (AC: 3)
  - [x] Add `read_directive()` → returns `DIRECTIVE.md` content (under the memory root) or `None` if absent. Read-only; this is the seam Story 4.4 injects first.
  - [x] **Core has NO write path to `DIRECTIVE.md`:** it is not a `MemoryOp` target and `apply_memory_op` can never write it. Make this structurally true (the op dispatch only ever targets `about.md`/`facts/`/`people/`), not just convention.

- [x] **Task 4: Tests** (AC: 1, 2, 3, 4)
  - [x] **AC1:** `apply_memory_op(RewriteAbout(...))` writes `about.md`; an invalid op (unknown type / missing field / bad `collection`) is rejected with no write; the write is atomic (simulate a crash before `os.replace` → prior file intact, mirror the 3.1 test). Injected `tmp_path` root — never real `$HOME`.
  - [x] **AC2:** after `rewrite_about`, the new `about.md` content reads back (persists). 
  - [x] **AC3:** `read_directive()` returns the file content when present, `None` when absent; assert **no memory-op writes `DIRECTIVE.md`** (apply every op type, confirm `DIRECTIVE.md` is untouched / not created); confirm core never targets it.
  - [x] **AC4:** `Remember(collection="people", name="Alex", ...)` creates `people/alex.md`; `collection="facts"` writes under `facts/`; a `name` with `../` or separators is sanitized/rejected (no escape).
  - [x] Extend the autouse conftest fixture to redirect the memory root off real `$HOME` (retro action #3, same change).

- [x] **Task 5: Verify guard + full suite** (AC: 1, 2, 3, 4)
  - [x] `uv run lint-imports` → both contracts KEPT (markdown layer is `core/` + stdlib; no provider import; AD-1 holds).
  - [x] `uv run pytest -q` → green (existing 209 unchanged + the new memory tests). No network, no real `$HOME`.

### Review Findings

- [x] [Review][Decision] Clarify whether `log_episode` is allowed to write `episodes.md` in Story 4.2 — the story is internally inconsistent: AC1/AC3 and the scope seam define core's write set as `about.md`/`facts/`/`people/`, but Task 2 also says `LogEpisode` should append an episodes record. The implementation currently writes `episodes.md`, so the code and spec cannot both be right as written.
  - **Resolved (owner decision 2026-06-17):** keep `log_episode` + `episodes.md` (it's one of AD-6's first three ops Task 1/2 require). Corrected the spec text — AC1, the scope seam, and the Task 2 path-safety subtask now list `episodes.md` in core's write set. No code change.
- [x] [Review][Decision] Define the `Remember.name` filename policy for collisions and non-ASCII names — `_slugify()` currently normalizes many distinct names to the same slug and rejects names that collapse to empty, so `Remember` can silently overwrite prior memories or fail on legitimate owner-mentioned names. The correct fix depends on whether this story wants rejection, transliteration, Unicode-preserving filenames, or another disambiguation rule.
  - **Resolved (owner decision 2026-06-17):** Unicode-preserving, path-safe sanitization. Replaced the ASCII `_slugify` with `_safe_filename` (NFC + casefold; collapse only non-`\w`/`-` runs) so `José`/CJK names persist instead of being rejected; same-name → same file (overwrite) is intended curation, consistent with `rewrite_about`/`add_face`. Added `test_remember_unicode_name_persists` and `test_remember_same_name_overwrites`.
- [x] [Review][Patch] Guard read accessors against non-file paths [shelldon/core/memory.py:135]
  - **Resolved:** `read_about`/`read_directive` now gate on `path.is_file()` (not `path.exists()`), so a directory at that path returns `None` instead of raising `IsADirectoryError`.

## Dev Notes

### Architecture compliance (binding)

- **AD-6 — curated markdown tree + fixed memory-op schemas:** "memory-ops have **fixed arg schemas in `contracts/`** (`remember`/`rewrite_about`/`log_episode`/`capture_learning`) — no free-text deltas"; "markdown = curated + durable." 4.2 builds the curated tree + the first three ops' apply path (`capture_learning` belongs to the `learnings`/dream work, Epic 6). [Source: ARCHITECTURE-SPINE.md#AD-6]
- **AD-5 — core is the sole writer; workers only propose:** "memory-ops have fixed arg schemas … core validates and applies." 4.2 is the **apply** half (core-only, synchronous). The **propose** half (worker → `Result`) is the recorded-decision follow-up. "atomic markdown writes (temp+rename)" is restated in the cross-cutting conventions. [Source: ARCHITECTURE-SPINE.md#AD-5, #State & cross-cutting]
- **AD-6 — `DIRECTIVE.md` disjoint writers:** the owner's authoritative constitution is read first and never written by the bot; core's write set (`about.md`/`facts/`/`people/`) is disjoint from the owner's (`DIRECTIVE.md`) — no writer conflict. 4.2 makes core's no-write-to-DIRECTIVE structural. [Source: ARCHITECTURE-SPINE.md#AD-6, epics.md#Story 4.2]
- **AD-10 — atomic-write crash-safety:** markdown writes use temp + `os.replace` (the M0 invariant 3.1 introduced). [Source: ARCHITECTURE-SPINE.md#AD-10]
- **AD-1 — LLM-free core:** the curated layer is pure file I/O in `core/`; import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]

### Recorded decision — the worker-proposes wire (built in a follow-up, NOT here)

- **Owner decision 2026-06-17 (Epic 4 planning gate):** the LLM completion becomes a structured memory-op via **worker-emits-Result** (Option A): the broker returns the completion to the worker; the worker parses its own reply into `Result.proposed_ops` and sends `Result → core`; core validates + applies via `apply_memory_op`. The broker stays a **pure egress/safety boundary** (no pet-domain parsing — AD-2); the worker is the **brain adapter** (assembles the prompt AND interprets the response — AD-3 symmetry).
- **This reshapes the fire-and-forget worker + the `RESULT → CORE` routing from Stories 1.5/1.8** — so it is its **own focused follow-up story**, not part of 4.2. **Story 3.4 (faces self-modify) rides the same wire** (it calls `apply_add_face`; memory-ops call `apply_memory_op`). 4.2 deliberately ships the apply half so the wire story is "just" the topology + parsing.

### Design guidance (what to build, minimally)

- **Mirror `apply_add_face` (3.3).** `apply_memory_op` is the same shape: a closed, validated, synchronous, core-only apply path that atomically persists. If you find yourself adding a bus message or worker call, stop — that's the follow-up wire.
- **`core/memory.py` for the curated layer.** Keep it flat alongside `core/history.py` (the sqlite layer). A `core/memory/` package holding both is a reasonable later tidy — don't move history now (scope).
- **Closed schemas, fixed args.** `forbid_unknown_fields=True` on the structs; `collection` is a closed `Literal`. Validate values (non-empty, in-range) beyond what msgspec structurally checks, and reject the whole op on any violation (no half-write) — the 3.1/3.3 discipline.
- **Path safety is load-bearing.** A `Remember.name` becomes a filename — slugify and reject traversal (`..`, `/`, absolute). A memory-op must be physically unable to write outside the tree (and never `DIRECTIVE.md`).
- **DIRECTIVE.md is read-only by construction.** No op type targets it; `read_directive()` only reads. Don't add a "write directive" path "for symmetry" — the disjointness is the point.
- **Inject the path; extend the conftest fixture in the same change** (retro action #3).

### What 4.2 does NOT do

- **No worker-proposes wire / no `Result.proposed_ops` / no topology reshape** — the recorded-decision follow-up (Option A). 4.2 defines the op schemas and the apply path; nothing emits ops over the bus yet.
- **No prompt injection / "memory shapes the turn"** — Story 4.4. 4.2 persists + exposes read accessors (`about.md`, `read_directive()`); nothing assembles a prompt.
- **No vault / uid isolation** — Story 4.3.
- **No `capture_learning` / `learnings` table / dream cycle** — AD-6 learnings + Epic 6.
- **No owner-facing editor for `about.md`** — it's bot-owned; the owner edits only `DIRECTIVE.md` (by hand, outside the app).
- **No `chat_id`/`user_id`** — single-owner (the history schema already shaped for it; not relevant to the markdown tree).

### Project Structure Notes

- **New:** `shelldon/core/memory.py` (curated markdown tree + `apply_memory_op` + `read_directive`, `DEFAULT_MEMORY_ROOT`, atomic-write reuse). Memory-op schemas added to `shelldon/contracts/__init__.py`. New tests `tests/test_memory.py`.
- **Modified:** `shelldon/contracts/__init__.py` (add the closed `MemoryOp` union — NOT yet on `Result`); `tests/conftest.py` (autouse fixture redirects the memory root to `tmp_path`). **`core/runtime.py` is likely untouched in 4.2** — `apply_memory_op` lives in `core/memory.py`; core wires to it when the follow-up wire delivers proposed ops. (If a thin `Core.apply_memory_op` passthrough helps the wire later, that's the wire story's call, not 4.2's.)
- `core/` + `contracts/` only → import-linter KEPT. The curated tree is the `core/ … memory/(owner)` of the Structural Seed. [Source: ARCHITECTURE-SPINE.md#Structural-Seed]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Inject a `tmp_path` memory root for every test — **never real `$HOME`** (extend the autouse fixture). Assert: op validation (accept/reject), atomic write (crash-before-replace leaves prior file), `about.md` persistence, `DIRECTIVE.md` read + never-written, `people/`/`facts/` placement, path-traversal rejection. Pure-ish apply path — test it directly with constructed ops (no sleeps). Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: epic-2-retro-2026-06-17.md, epic-3-retro-2026-06-17.md]

### Previous story intelligence (Epic 3 + Story 4.1)

- **`apply_add_face` (3.3) is the template** for `apply_memory_op`: closed-schema validate → reject-without-writing → atomic persist → core sole writer. Reuse the shape and the atomic-write helper. [Source: shelldon/core/faces.py]
- **4.1 just shipped the history half of AD-6** (sqlite). 4.2 is the curated-markdown half. Keep them separate modules; both are core-owned, single-writer, `~/.shelldon/`-rooted, conftest-isolated. [Source: shelldon/core/history.py]
- **Recurring review themes to pre-empt:** guard inputs (path safety, closed `collection`); never silently swallow (log on a skipped/odd op, raise on invalid); no WHAT-comments; share test helpers via conftest; value-not-truthiness asserts; proactive `$HOME` isolation in the same change. [Source: epic-3-retro-2026-06-17.md, 4-1 Review Findings]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 4 / Story 4.2 (this story); #Story 4.1 (sqlite history — done); #Story 4.3 (vault — later); #Story 4.4 (memory shapes the turn — consumes these reads); #Story 3.4 (faces self-modify — rides the same wire follow-up)]
- [Source: ARCHITECTURE-SPINE.md#AD-6 (curated markdown, fixed memory-op schemas, DIRECTIVE.md disjoint writers), #AD-5 (core sole writer, workers propose), #AD-10 (atomic write), #AD-1 (LLM-free core), #AD-2 (broker pure egress — why the worker, not the broker, parses ops), #AD-3 (worker assembles prompt — symmetry for parsing the response), #Structural-Seed (`core/ … memory/`)]
- [Source: shelldon/core/faces.py (`add_face`/`apply_add_face` — the apply-path template; `_atomic_write_text`), shelldon/core/state.py (atomic checkpoint), shelldon/core/history.py (4.1 store conventions)]
- [Source: shelldon/contracts/__init__.py (closed/frozen msgspec struct + tagged-union precedent for the memory-op schemas)]
- [Source: tests/conftest.py (`_isolate_state_checkpoint` autouse fixture to extend for the memory root)]
- [Source: _bmad-output/implementation-artifacts/epic-3-retro-2026-06-17.md (retro action #3 proactive `$HOME` isolation; recurring review themes); owner decision 2026-06-17 (topology = worker-emits-Result; 4.2 split)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

None — clean TDD run. Baseline suite green (209), new memory tests green (19) on first full run, full suite 228 green, both import-linter contracts KEPT.

### Implementation Plan

Mirrored `faces.apply_add_face` (Story 3.3) as instructed: a closed-schema → validate → reject-without-writing → atomic-persist → core-sole-writer apply path.

1. **contracts/** — added three frozen, tagged, `forbid_unknown_fields` structs (`Remember`/`RewriteAbout`/`LogEpisode`) + the `MemoryOp` union. NOT attached to `Result`/`Envelope` (that's the 4.5 wire).
2. **core/memory.py** — `CuratedMemory` (rooted, injectable; parallel to `HistoryStore`). `apply_memory_op` dispatches by op type; only ever targets `about.md`/`facts/`/`people/`/`episodes.md`. Reused the AD-10 atomic-write idiom (temp + fsync + `os.replace`) verbatim from faces.py/state.py — the codebase already duplicates this helper per module, so matched that convention rather than refactoring a shared one (out of scope).
3. **Path safety** — `_slugify` reduces a name to `[a-z0-9-]` (so `../etc` → `etc`, can never carry a separator); empty slug is rejected; a belt-and-suspenders `path.parent == collection_dir` check makes "physically unable to escape" structural.
4. **DIRECTIVE.md** — read-only `read_directive()`; no dispatch branch names it (disjoint writers, structural not conventional).
5. **conftest** — extended the autouse fixture to redirect `DEFAULT_MEMORY_ROOT` off real `$HOME` in the same change (Epic 3 retro #3).

### Completion Notes List

- All 4 ACs satisfied. `apply_memory_op` is the validated, synchronous, core-only **apply half** of AD-5; the worker-proposes wire (`Result.proposed_ops`) is deliberately NOT built here (Story 4.5, sprint-status `backlog`).
- Validation is whole-reject: bad `collection` (Literal isn't enforced on direct struct construction, so core re-validates), empty content, empty-slug name, and non-`MemoryOp` objects all raise `ValueError` with nothing written.
- Closed schemas proven both ways: a typo'd field and a typo'd op tag (`remembr`) are msgspec decode errors; each op round-trips back to its own type by tag.
- Disjoint-writer guarantee tested directly: every op type applied → `DIRECTIVE.md` never created/touched; an owner-authored `DIRECTIVE.md` survives core's writes.
- Scope held: no wire, no prompt injection (4.4), no vault (4.3), no `capture_learning`/learnings (Epic 6). `core/runtime.py` untouched.

### File List

- **Added:** `shelldon/core/memory.py` — curated markdown tree, `CuratedMemory.apply_memory_op`, `read_about`, `read_directive`, `DEFAULT_MEMORY_ROOT`, atomic-write + slug helpers.
- **Added:** `tests/test_memory.py` — 19 tests across AC1–AC4 + the closed-schema and LogEpisode behaviours.
- **Modified:** `shelldon/contracts/__init__.py` — added `Remember`/`RewriteAbout`/`LogEpisode`/`MemoryOp` (+ `__all__`, `Literal` import); NOT attached to `Result`/`Envelope`.
- **Modified:** `tests/conftest.py` — autouse fixture redirects `DEFAULT_MEMORY_ROOT` to `tmp_path` (retro #3).
- **Modified (workflow bookkeeping):** this story file; `_bmad-output/implementation-artifacts/sprint-status.yaml`.

### Change Log

- 2026-06-17 — Implemented Story 4.2: closed memory-op schemas in `contracts/` + the core-only curated-markdown apply path (`CuratedMemory`) with atomic writes, path-traversal safety, and the read-only disjoint-writer `DIRECTIVE.md` accessor. Suite 228 green; import contracts KEPT.
- 2026-06-17 — Addressed code review findings — 3 items resolved (2 decisions + 1 patch): kept `log_episode`/`episodes.md` and corrected the spec write-set text (owner decision); switched to Unicode-preserving path-safe filenames so non-ASCII names persist (`_safe_filename`, owner decision); guarded `read_about`/`read_directive` on `is_file()`. +2 tests; suite 230 green; contracts KEPT.
