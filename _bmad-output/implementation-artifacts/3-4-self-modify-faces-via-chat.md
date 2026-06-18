---
baseline_commit: 3d0e70647fd679d6fb50f2dfc7643709b316442d
---

# Story 3.4: Self-modify faces via chat

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want to tell the pet (in chat) to add or tweak a face, and have it do so,
so that its expressions grow with it — the v1 capability I loved, made safe under single-writer core (CAP, AD-5, AD-7).

## Acceptance Criteria

1. **The worker can propose a structured `add_face` op on its `Result` (no creds, no direct write):** Given an owner message asking for a new/changed face, when the turn runs, then the worker parses an `add_face` op from its reply into `Result.proposed_ops` (the closed proposed-ops list from Story 4.5) — carrying the face's name + mood ranges + optional token, **no credentials and no direct write** (workers never write — AD-5). `add_face` is added to the closed `ProposedOp` union in `contracts/`; an unknown/typo'd op or malformed field is a decode error (the reply is unaffected — whole-block reject, Story 4.5).
2. **Core validates and applies it via Story 3.3's `apply_add_face`, rejecting a malformed proposal without mutating anything:** Given a proposed `add_face`, when core receives the `Result` and applies `proposed_ops`, then core dispatches it to the existing `Core.apply_add_face` (atomic, comment-preserving `faces.toml` write — Story 3.3) as the **sole writer** (AD-5). A malformed/duplicate proposal (bad range, empty name, duplicate without `replace`) is **rejected without mutating RAM or disk** (the 3.3 whole-reject discipline), logged + skipped, never crashing the turn and never affecting the reply (Story 4.5 guard). The new face is **selectable on the next mood match** (`faces.select`).
3. **The 4.5 write-back machinery is reused, not re-built:** Given the `Result`-carries-proposed-ops + core-validates-and-applies wire built in Story 4.5, when `add_face` is added, then it **reuses** that machinery (the same `proposed_ops` field, the same worker parse, the same fenced apply loop) — 3.4 only adds the op type + a dispatch branch. No new wire, no topology change.

> **Scope seam (binding):** 3.4 is the thin face-op add onto the Story 4.5 wire — the dispatch branch I deliberately left open (`# Story 3.4 inserts a branch here`). It builds: the `AddFace` struct in `contracts/`, its inclusion in the closed `ProposedOp` union (so the worker decoder + `Result.proposed_ops` carry it), and the `isinstance(op, AddFace) → self.apply_add_face(...)` branch in `core/_apply_proposed_ops`. It does **NOT** build: the **prompt** that makes the LLM emit an `add_face` (Story 4.4 owns prompt assembly — 3.4 tests the parse + apply against a canned reply, exactly as 4.5 did); any **new face schema/validation** (3.3's `add_face` already validates name + ranges + duplicate); any **face-deletion / face-tuning UI** beyond `replace=True` (out of scope). The single biggest mistake is re-touching the 4.5 wire/topology or building 4.4's prompt here.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent) — verified against the code:**
> - **Story 4.5 left the exact seam for this.** `core/runtime.py:_apply_proposed_ops` loops `proposed_ops` guarded (cap + try/except log-and-skip) and has the marker `# Story 3.4 inserts a branch here: an AddFace op → self.apply_add_face(...).` — insert the branch there. [Source: shelldon/core/runtime.py:359-374]
> - **`Core.apply_add_face` already exists and validates** — `apply_add_face(name, **kwargs)` → `faces.add_face(name, valence=, arousal=, energy=, token=, replace=)`, which validates (non-empty name, in-range well-ordered tuples, duplicate-unless-replace) and atomically rewrites `faces.toml` preserving comments, raising `ValueError` on any violation. The 4.5 guard already catches that ValueError → log + skip. [Source: shelldon/core/runtime.py:347-351, shelldon/core/faces.py:add_face/_validate_face]
> - **`proposed_ops` is already on `Result`** as `list[MemoryOp]` defaulting to empty (Story 4.5). 3.4 widens the element type to a `ProposedOp` union (memory-ops + the face op) — an additive type change, still defaulting empty, still no `SCHEMA_VERSION` bump (AD-13). [Source: shelldon/contracts/__init__.py (Result.proposed_ops)]
> - **The worker already parses a fenced ```ops block** into the proposed-ops list via `_OPS_DECODER = msgspec.json.Decoder(list[MemoryOp])` with whole-block reject on a malformed/unknown op. 3.4 only changes the decoder's element type to `ProposedOp` so an `add_face` object decodes. [Source: shelldon/worker/worker.py:44, :53-77]
> - **`Face` shape (the add_face args):** `name: str`, `valence/arousal/energy: tuple[float,float]` (ranges, in [-1,1]/[-1,1]/[0,1]), `token: str = ""`. `add_face` also takes `replace: bool = False`. The `AddFace` op mirrors these fields exactly. [Source: shelldon/core/faces.py:Face, add_face]
> - **`MemoryOp` lives above `Result`** in `contracts/` (moved there in 4.5 so `Result` can reference it). Define `AddFace` + the `ProposedOp` union in the same block (before `Result`). [Source: shelldon/contracts/__init__.py (Memory-ops section)]

- [x] **Task 1: `AddFace` op + `ProposedOp` union in `contracts/`** (AC: 1, 3)
  - [x] Define a frozen, tagged (`"add_face"`), `forbid_unknown_fields` `AddFace` struct mirroring `add_face`'s args: `name: str`, `valence: tuple[float, float]`, `arousal: tuple[float, float]`, `energy: tuple[float, float]`, `token: str = ""`, `replace: bool = False`. Place it in the memory-ops block (before `Result`).
  - [x] Add `ProposedOp = MemoryOp | AddFace` (the closed union of everything a worker may propose). Change `Result.proposed_ops` from `list[MemoryOp]` to `list[ProposedOp]` (still `default_factory=list`; no `SCHEMA_VERSION` bump). Keep `MemoryOp` as the memory-only union (it's what `CuratedMemory.apply_memory_op` handles). Add `AddFace`/`ProposedOp` to `__all__`.

- [x] **Task 2: Worker parses `add_face` ops** (AC: 1)
  - [x] Change the worker's `_OPS_DECODER` to `msgspec.json.Decoder(list[ProposedOp])` and update `parse_reply`'s return annotation. No other worker change — the fenced-block parse + whole-block reject already handle it. The worker still never writes (AD-5) and imports only the contract type.

- [x] **Task 3: Core dispatches `AddFace` → `apply_add_face`** (AC: 2)
  - [x] In `core/runtime.py:_apply_proposed_ops`, replace the seam marker with a branch: `if isinstance(op, AddFace): self.apply_add_face(op.name, valence=op.valence, arousal=op.arousal, energy=op.energy, token=op.token, replace=op.replace)` else `self.apply_memory_op(op)`. The existing try/except keeps a malformed `AddFace` (ValueError from `add_face`) logged + skipped — no mutation, no turn crash, reply unaffected. Import `AddFace` from contracts.

- [x] **Task 4: Tests** (AC: 1, 2, 3)
  - [x] **AC1 (parse):** a canned reply with an ```ops block containing an `add_face` object → `parse_reply` yields an `AddFace` with the right fields; an `add_face` with a malformed field (e.g. a 3-element range) fails the whole block (no ops, reply intact).
  - [x] **AC2 (apply):** a `Result` carrying a valid `AddFace` → core adds the face to the registry (it's in `core.faces.faces` and `core.faces.select(...)` returns its token on a matching mood), written atomically to the injected `faces.toml`; a malformed/duplicate `AddFace` → registry unchanged, reply still delivered, turn survives (fence idle).
  - [x] **AC3 (reuse):** a mixed `proposed_ops` batch (an `AddFace` + a `Remember`) → the face goes to the registry AND the memory-op goes to the curated tree, proving both dispatch paths off the one wire.
  - [x] **Contract:** an `AddFace` inside `Result.proposed_ops` round-trips encode→decode back to the `AddFace` type (extend `test_contracts_roundtrip.py`); `SCHEMA_VERSION` unchanged.

- [x] **Task 5: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (worker imports only the contract type; core unchanged re: LLM-free).
  - [x] `uv run pytest -q` → green (existing suite + the new face-op tests; no topology test changes needed — 4.5's wire is untouched).

## Dev Notes

### Architecture compliance (binding)

- **AD-5 — core is the sole writer; workers only propose.** The worker proposes `AddFace` on its `Result`; core applies it via `apply_add_face`. The worker never writes `faces.toml`. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-7 — the faces registry is self-modifiable, corruption-tolerant, atomically written.** `add_face` (Story 3.3) already enforces the closed face schema and the atomic comment-preserving write. 3.4 reaches it from a real turn. [Source: ARCHITECTURE-SPINE.md#AD-7, shelldon/core/faces.py]
- **AD-6 / Story 4.5 — proposed-ops wire.** 3.4 reuses the exact `Result.proposed_ops` + worker-parse + fenced-apply machinery; it only widens the closed op union and adds one dispatch branch. No topology change. [Source: epics.md#Story 4.5, #Story 3.4]
- **AD-13 — additive wire change, no version bump.** Widening `proposed_ops`'s element type to include `AddFace` keeps the empty default; old plain replies are unaffected. [Source: ARCHITECTURE-SPINE.md#AD-13]
- **AD-1 — LLM-free core.** The dispatch is pure in-core logic; the parse lives in `worker/`. Import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]

### Design guidance (what to build, minimally)

- **This is genuinely thin — three edits + tests.** If you find yourself touching the COMPLETION hop, the fence, the broker, or the worker's connection lifecycle, stop — none of that changes (that was 4.5).
- **`AddFace` is a face op, not a memory-op.** Keep `MemoryOp` (what `CuratedMemory` handles) separate from `ProposedOp` (everything a worker proposes). Core branches on `isinstance(op, AddFace)`; everything else goes to `apply_memory_op`. This is exactly why 4.5 named the union `MemoryOp` and left the dispatch open.
- **Validation is already done by 3.3.** Don't re-validate ranges in core or contracts beyond what the struct types + `add_face` enforce; rely on `add_face` raising `ValueError`, which the 4.5 guard already catches and logs.
- **No prompt work.** The LLM won't emit `add_face` until Story 4.4 designs the prompt; 3.4 proves the parse+apply with a canned reply (same approach 4.5 used). The reply→ops format is co-owned with 4.4.

### What 3.4 does NOT do

- **No prompt assembly / no instructing the LLM to emit `add_face`** — Story 4.4.
- **No new wire / topology / fence change** — Story 4.5 owns all of that.
- **No new face validation** — Story 3.3's `add_face` is the validator.
- **No face deletion / batch face editing UI** — only add/replace via the op.

### Project Structure Notes

- **Modified:** `shelldon/contracts/__init__.py` (`AddFace` struct, `ProposedOp` union, `Result.proposed_ops` element type, `__all__`); `shelldon/worker/worker.py` (`_OPS_DECODER` element type + annotation); `shelldon/core/runtime.py` (`_apply_proposed_ops` dispatch branch + `AddFace` import).
- **Tests:** extend `tests/test_contracts_roundtrip.py`; new face-op tests (fold into `tests/test_proposed_ops.py` or a new `tests/test_self_modify_faces.py`). The conftest already redirects `DEFAULT_FACES_PATH` off `$HOME`, so a Core in tests writes `faces.toml` to `tmp_path` — no new isolation needed.
- `core/` + `contracts/` + `worker/` boundaries unchanged → import-linter KEPT. [Source: pyproject.toml#tool.importlinter]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Drive core-apply by putting a crafted `Result` (with an `AddFace` in `proposed_ops`) into the open turn via `_handle_result` (the 4.5 `test_proposed_ops.py` pattern — no real worker needed). Assert: the face is in `core.faces.faces` + selectable via `core.faces.select(...)`; a malformed/duplicate `AddFace` leaves the registry unchanged and the turn alive; the worker `parse_reply` decodes an `add_face` block; the contract round-trips an `AddFace` in `proposed_ops`. Before done: `uv run lint-imports` (KEPT) + `uv run pytest -q` (green). [Source: tests/test_proposed_ops.py, tests/test_faces.py]

### Previous story intelligence (Story 3.3 + 4.5)

- **3.3 shipped `add_face`/`apply_add_face`** — the validated, atomic, comment-preserving registry writer. The catch-all `content` face must stay last; `add_face` already inserts before a trailing catch-all so selection still resolves. [Source: shelldon/core/faces.py]
- **4.5 shipped the proposed-ops wire** (`Result.proposed_ops`, the worker fenced-block parse, `_apply_proposed_ops` with cap + guard) and **named the dispatch seam** for this story. The cap (`MAX_PROPOSED_OPS`) and the log-and-skip guard already cover an `AddFace` too. [Source: shelldon/core/runtime.py, shelldon/worker/worker.py]
- **Recurring review themes to pre-empt:** validate via the existing path (don't duplicate range checks); never silently swallow (the guard logs a rejected op); value-not-truthiness asserts; reuse the 4.5 test patterns + conftest isolation. [Source: epic-3-retro-2026-06-17.md, 4-5 Review Findings]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 3.4 (this story); #Story 3.3 (apply_add_face — done); #Story 4.5 (the proposed-ops wire — done, seam left for this); #Story 4.4 (prompt assembly — will emit the add_face)]
- [Source: ARCHITECTURE-SPINE.md#AD-5 (sole writer), #AD-7 (self-modifiable faces, atomic), #AD-6 (proposed ops), #AD-13 (additive wire), #AD-1 (LLM-free core)]
- [Source: shelldon/core/runtime.py (`_apply_proposed_ops` seam, `apply_add_face`), shelldon/core/faces.py (`add_face`/`_validate_face`/`Face`), shelldon/worker/worker.py (`_OPS_DECODER`/`parse_reply`), shelldon/contracts/__init__.py (`MemoryOp`/`Result.proposed_ops`)]
- [Source: tests/test_proposed_ops.py (4.5 apply-path test pattern), tests/test_faces.py (registry assertions), tests/test_contracts_roundtrip.py (op round-trip)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

None — clean run. The story was as thin as scoped: 3 source edits + tests, no topology/wire/fence change. Suite 251 green (was 244), both import contracts KEPT on first run.

### Implementation Plan

Filled the dispatch seam Story 4.5 left open. Three source edits:

1. **contracts** — added the frozen tagged `AddFace` struct (mirrors `add_face`'s args: name + valence/arousal/energy tuples + token + replace), and a `ProposedOp = MemoryOp | AddFace` union. Changed `Result.proposed_ops` from `list[MemoryOp]` to `list[ProposedOp]` (still empty-default, no `SCHEMA_VERSION` bump — AD-13). Kept `MemoryOp` as the memory-only union (what `CuratedMemory` handles). `__all__` updated.
2. **worker/worker.py** — `_OPS_DECODER` element type → `list[ProposedOp]` (+ `parse_reply` annotation). The fenced-block parse + whole-block reject already handle the new op type; no other worker change.
3. **core/runtime.py** — replaced the `# Story 3.4 inserts a branch here` marker with `isinstance(op, AddFace) → self.apply_add_face(...)`, else `apply_memory_op`. The 4.5 cap + try/except guard already covers a malformed `AddFace` (ValueError from 3.3's `add_face`) → log + skip.

### Completion Notes List

- **All 3 ACs satisfied.** AC1: the worker parses an `add_face` op from a fenced ```ops block into `Result.proposed_ops` (no creds, no write). AC2: core dispatches it to 3.3's `apply_add_face` (atomic faces.toml write); a malformed/out-of-range proposal is rejected without mutating the registry and the turn survives; the new face is selectable on the next mood match (`faces.select`). AC3: a mixed `AddFace` + `Remember` batch routes each op to its writer — the 4.5 machinery is reused, not rebuilt.
- **Design call:** `AddFace` is a face op, NOT a memory-op — so `MemoryOp` (what `CuratedMemory.apply_memory_op` accepts) stays separate from the broader `ProposedOp` union, and core branches on `isinstance(op, AddFace)`. This is exactly why 4.5 named the memory union `MemoryOp` and left the dispatch open.
- **No new validation** — relied entirely on 3.3's `add_face`/`_validate_face` (name, in-range well-ordered tuples, duplicate-unless-replace) and the 4.5 guard, per the whole-reject discipline.
- **Scope held:** no prompt work (4.4 will make the LLM emit `add_face`; tested here with canned replies), no wire/topology/fence change (4.5 owns it), no face-deletion UI. `broker/` untouched.

### File List

- **Modified:** `shelldon/contracts/__init__.py` — `AddFace` struct, `ProposedOp` union, `Result.proposed_ops` element type, `__all__`.
- **Modified:** `shelldon/worker/worker.py` — `_OPS_DECODER`/`parse_reply` element type → `ProposedOp`.
- **Modified:** `shelldon/core/runtime.py` — `_apply_proposed_ops` dispatch branch + `AddFace` import.
- **Modified (tests):** `tests/test_proposed_ops.py` (face-op parse + core apply + mixed-batch), `tests/test_contracts_roundtrip.py` (`AddFace` round-trip in `proposed_ops`).
- **Modified (workflow bookkeeping):** this story file; `_bmad-output/implementation-artifacts/sprint-status.yaml`.

### Review Findings

- [x] [Review][Patch] Missing AC2 rejection tests: no test for empty-name `AddFace` and no test for duplicate-name without `replace=True` — both named as rejection cases in AC2 [tests/test_proposed_ops.py]
  - **Resolved:** added `test_core_rejects_empty_name_add_face` and `test_core_rejects_duplicate_add_face_without_replace` — both assert the registry is unchanged (not mutated/duplicated/reordered) and the turn survives. The two explicit AC2 rejection cases are now covered alongside the existing out-of-range case.
- [x] [Review][Defer] Whitespace-only face name passes `_validate_face` (`not face.name` is False for `"   "`) [shelldon/core/faces.py:88] — deferred, pre-existing
- [x] [Review][Defer] Point-range `lo==hi` silently accepted — `_validate_range` checks `lo > hi` only, not equal [shelldon/core/faces.py:79] — deferred, pre-existing
- [x] [Review][Defer] `replace=True` on catch-all `content` face replaces it in-place, breaking the "broadest last" invariant without warning [shelldon/core/faces.py:215] — deferred, pre-existing
- [x] [Review][Defer] No per-field size limit on `token`/`name` in `add_face` — a worker can bloat `faces.toml` arbitrarily within LLM output limits [shelldon/core/faces.py:193] — deferred, pre-existing
- [x] [Review][Defer] All-ops reply (worker proposes ops but says nothing else) produces an empty `payload` string silently delivered to the user [shelldon/worker/worker.py:parse_reply] — deferred, pre-existing from 4.5

### Change Log

- 2026-06-17 — Implemented Story 3.4: self-modify faces via chat. Added the `AddFace` op to the closed `ProposedOp` union; the worker parses it from its reply and core dispatches it to Story 3.3's `apply_add_face` (sole writer, atomic), guarded + capped by the Story 4.5 wire. The first face op rides the proposed-ops machinery — no new wire. Suite 251 green; import contracts KEPT.
- 2026-06-18 — Addressed code review: 1 patch resolved (added the two missing AC2 rejection tests — empty-name + duplicate-without-replace; registry-unchanged + turn-survives asserts), 5 lower-priority items deferred (pre-existing faces.py/4.5 behaviors). Suite 253 green; contracts KEPT.
