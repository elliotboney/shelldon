---
baseline_commit: 455322828befff768017bd60430ba0f44f157015
---

# Story 10.1: Persona files + seed-on-boot + prompt reads them (the foundation)

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner of shelldon,
I want shelldon's character to live in editable markdown files in the writable memory tree instead of a hardcoded Python constant,
so that who shelldon *is* can change without editing source or redeploying — the v1/openclawgotchi worktree-prompt model, rebuilt inside v2's invariants.

## Acceptance Criteria

**AC1 — Seed templates ship in the repo**
**Given** a fresh checkout
**When** the source tree is inspected
**Then** a new package dir `shelldon/persona/` exists holding pristine seed templates: `BOT_INSTRUCTIONS.md` (carrying today's `SYSTEM_INSTRUCTION` text **verbatim**), plus `SOUL.md`, `IDENTITY.md`, `USER.md`
**And** these `.md` files are included in the built wheel (packaging verified, not just present on disk)

**AC2 — Seed-on-absent into the writable memory root**
**Given** a memory root with no persona files
**When** `CuratedMemory` is initialized
**Then** each missing persona file is copied from the shipped templates into the memory root (the `faces.FaceRegistry.load` absent→seed idiom, made copy-if-absent)
**And** the write is atomic (temp + fsync + `os.replace`, the AD-10 `_atomic_write_text` recipe)

**AC3 — Seed-skip on present (never overwrite owner edits)**
**Given** a memory root where a persona file already exists (e.g. an owner hand-edited `BOT_INSTRUCTIONS.md`)
**When** `CuratedMemory` is initialized
**Then** the existing file is left **untouched** — seeding only fills absent files, never overwrites

**AC4 — Prompt reads the persona files**
**Given** seeded persona files in the memory root
**When** `gather_context` runs and `assemble_prompt` composes the prompt
**Then** `BOT_INSTRUCTIONS.md` content drives the system slot (replacing the deleted `SYSTEM_INSTRUCTION` constant), and `SOUL.md`/`IDENTITY.md`/`USER.md` are injected in the binding AD-6 order: `BOT_INSTRUCTIONS (system) → DIRECTIVE → IDENTITY → SOUL → USER → about → knowledge → summary → recent → recall → owner_message`
**And** each persona section is char-budgeted (reuse the `KNOWLEDGE_CHAR_BUDGET` discipline) so a runaway file can't blow the 416MB-box context

**AC5 — The `SYSTEM_INSTRUCTION` constant is deleted**
**Given** the codebase after this story
**When** `shelldon/worker/prompt.py` is searched
**Then** the `SYSTEM_INSTRUCTION` module constant no longer exists; the only source of the system text is `BOT_INSTRUCTIONS.md` (seeded from the repo template)

**AC6 — Fail-soft on every persona read**
**Given** a missing or corrupt (non-UTF-8 / unreadable) persona file
**When** `gather_context` assembles the prompt
**Then** that section is omitted and the failure is logged at WARNING — never raised into the turn (worst case degrades to owner message only, matching today's `about.md`/`DIRECTIVE.md` discipline)
**And** seed-on-absent never overwrites a present file on the degrade path

**AC7 — Day-one behavior is a no-op (golden test)**
**Given** the seed templates as shipped (BOT_INSTRUCTIONS verbatim; SOUL/IDENTITY/USER ship **empty** so the existing omit-if-empty rule drops them)
**When** the prompt is assembled from seed files
**Then** the assembled prompt is **byte-identical** to the prior hardcoded-constant prompt — moving the constant to a file changes nothing observable on day one

## Tasks / Subtasks

- [x] **Task 1 — Create `shelldon/persona/` seed templates** (AC: 1, 7)
  - [x] Create `shelldon/persona/__init__.py` (empty — makes it an importable subpackage so `importlib.resources.files("shelldon.persona")` works under zip/wheel installs)
  - [x] Create `shelldon/persona/BOT_INSTRUCTIONS.md` containing the current `SYSTEM_INSTRUCTION` string **verbatim** (exact text, exact newlines — this is load-bearing for the AC7 golden test)
  - [x] Create `shelldon/persona/SOUL.md`, `IDENTITY.md`, `USER.md` as **empty** files (zero bytes, or a single trailing newline). Rationale: empty → `read` returns falsy → omit-if-empty drops the section → AC7 byte-parity holds. Real persona content is populated by Story 10.4 onboarding; these exist now only so copy-if-absent, 10.2 rewrite-ops, and bot-awareness have files to target.
  - [x] Verify wheel packaging: `uv build` (or `python -m build`) then confirm the `.md` files appear inside the wheel (`unzip -l dist/*.whl | grep persona`). hatchling `packages = ["shelldon"]` should include them — confirm, don't assume.

- [x] **Task 2 — Seed-on-init in `CuratedMemory`** (AC: 2, 3, 6)
  - [x] Add a module constant naming the persona file set, e.g. `_PERSONA_SEED_FILES = ("BOT_INSTRUCTIONS.md", "SOUL.md", "IDENTITY.md", "USER.md")`
  - [x] Add a `_seed_persona()` helper called from `CuratedMemory.__init__`: for each persona file, if absent in `self._root`, read the packaged template via `importlib.resources.files("shelldon.persona").joinpath(name).read_text()` and write it atomically with the existing `_atomic_write_text`. Present files are skipped.
  - [x] Fail-soft: a seed failure (template unreadable, disk error) logs at WARNING and is swallowed — `CuratedMemory()` construction must never raise (it runs at boot in `runtime.py:311` and per-fork in `prompt.py:168`). A failed seed just means that section degrades later (AC6).
  - [x] **Idempotency / fork-safety note:** `CuratedMemory()` is constructed both at core boot AND inside every fork worker (`prompt.py:168`). Seed-on-init runs in both, but copy-if-absent makes the worker path a near-no-op after the first boot (files already present). Confirm no write-contention concern — the worker only ever *adds* an absent file, never rewrites; core remains sole writer of present files (AD-5 intact).

- [x] **Task 3 — Read accessors for persona files** (AC: 4, 6)
  - [x] Add `read_soul()`, `read_identity()`, `read_user()`, and `read_instructions()` to `CuratedMemory` — mirror `read_about` exactly: `path.read_text() if path.is_file() else None`. (No write/apply paths in this story — those are Story 10.2.)

- [x] **Task 4 — Wire persona into `gather_context` + `assemble_prompt`** (AC: 4, 5, 6, 7)
  - [x] In `gather_context`: inside the existing `try` that reads directive/about/summary/knowledge, also read `instructions`, `identity`, `soul`, `user` via the new accessors. They share the existing `(OSError, UnicodeError)` fail-soft handler — a corrupt file degrades the whole memory read exactly as today (or split per-file if finer-grained degrade is wanted; match the existing coarse handler for minimal change).
  - [x] Char-budget each persona section. Simplest: reuse the existing `_bounded_knowledge`-style cap, or a small `_bounded_text(text, budget)` helper applied to each. Pick the lower-footprint option that matches existing style (don't over-engineer).
  - [x] In `assemble_prompt`: add `instructions=`, `identity=`, `soul=`, `user=` params. Use `instructions` (falling back to nothing if absent) as the `system` slot. Insert IDENTITY/SOUL/USER sections in the binding order **after DIRECTIVE, before about**. Every new section omits-if-empty (no empty headers), owner message stays last.
  - [x] **Delete the `SYSTEM_INSTRUCTION` constant** and its `system=SYSTEM_INSTRUCTION` default. `assemble_prompt`'s `system` now comes from the passed-in instructions (default `None`/absent → no system section, but in practice always seeded). Update the module docstring (lines 1-16 and 41-44) to describe file-sourced persona instead of the hardcoded constant.
  - [x] Verify `build_prompt` still threads everything through (it spreads `**gather_context(...)` into `assemble_prompt`, so new keys flow automatically once both sides agree on names).

- [x] **Task 5 — Tests** (AC: all)
  - [x] `seed-on-absent`: empty `tmp_path` root → `CuratedMemory(root)` → all four persona files exist on disk with the template content.
  - [x] `seed-skip-on-present`: pre-write a sentinel `BOT_INSTRUCTIONS.md` → construct → file unchanged (sentinel survives).
  - [x] `assembly order`: with all sections populated, assert the section order matches the binding AD-6 order and owner message is last.
  - [x] `degrade path`: a corrupt (non-UTF-8 bytes) `SOUL.md` → section omitted, WARNING logged, turn proceeds, other sections still present.
  - [x] **`golden / day-one no-op` (AC7):** assert the prompt assembled from seed files (BOT_INSTRUCTIONS verbatim + empty SOUL/IDENTITY/USER) is byte-identical to the prompt the prior `SYSTEM_INSTRUCTION` constant produced for the same inputs. Capture the prior expected string as a golden constant in the test.
  - [x] Confirm the full suite stays green (537+ tests baseline) and import-linter still passes (`core/` stays LLM-free — persona is data, not model code).

## Dev Notes

### What this story is (and is NOT)
- **IS:** persona moves from a code constant into seed `.md` files, seeded copy-if-absent into `~/.shelldon/memory/`, read into the prompt in the new AD-6 order, constant deleted, day-one parity proven by a golden test.
- **IS NOT:** no `rewrite_*` ops, no contracts changes, no bot writability, no onboarding, no proactive/dream changes, no caching. Those are Stories 10.2–10.5. Resist scope creep — this is the foundation only.

### Files to touch
| File | Change | Type |
|---|---|---|
| `shelldon/persona/__init__.py` | new empty subpackage marker | NEW |
| `shelldon/persona/BOT_INSTRUCTIONS.md` | verbatim `SYSTEM_INSTRUCTION` text | NEW |
| `shelldon/persona/SOUL.md` / `IDENTITY.md` / `USER.md` | empty seed placeholders | NEW |
| `shelldon/core/memory.py` | seed-on-init + `read_soul/identity/user/instructions` accessors | UPDATE |
| `shelldon/worker/prompt.py` | read persona, inject in order, **delete `SYSTEM_INSTRUCTION`** | UPDATE |
| `tests/test_memory.py` | seed-on-absent / seed-skip / degrade tests | UPDATE |
| `tests/test_prompt_assembly.py` | assembly order + golden no-op test | UPDATE |

### Existing patterns to MIRROR (do not reinvent)
- **Seed-on-absent idiom:** `shelldon/core/faces.py:168-188` — `FaceRegistry.load` writes the starter set to disk when the file is absent, falls back on corruption, never raises. Your `_seed_persona` is the copy-if-absent analogue (per-file, not whole-doc).
- **Atomic write:** `shelldon/core/memory.py:48-65` `_atomic_write_text` (temp in same dir → fsync → `os.replace`) — reuse it directly; do NOT write a second atomic writer.
- **Read accessor shape:** `shelldon/core/memory.py:147-151` `read_about` — `path.read_text() if path.is_file() else None`. Your four new accessors are exact copies with different filenames.
- **Fail-soft read in assembly:** `shelldon/worker/prompt.py:167-179` — the `(OSError, UnicodeError)` handler already degrades `about`/`directive`/`summary`. `UnicodeError` MUST stay listed explicitly (it subclasses `ValueError`, not `OSError` — see the comment at `prompt.py:176-178`); your new reads ride the same handler.
- **Char-budget discipline:** `shelldon/worker/prompt.py:217-228` `_bounded_knowledge` + `KNOWLEDGE_CHAR_BUDGET` — drop-with-logged-count, never silent truncation.
- **Omit-if-empty assembly:** `shelldon/worker/prompt.py:128-147` — every section guards on `x and x.strip()`; replicate for the new sections. This is exactly why empty SOUL/IDENTITY/USER seeds produce day-one parity.

### Current state of the files being modified (read before editing)
- **`worker/prompt.py`** — `SYSTEM_INSTRUCTION` is a multi-line constant at lines 45-95 (the ONLY LLM-facing copy today; it embeds THOUGHT:/FACE: contract, ops fences, tool copy). `assemble_prompt` (112-147) composes `system → directive → about → knowledge → summary → recent → recall → owner_message`. `gather_context` (150-214) opens read-only handles, fail-soft. `build_prompt` (231-235) = gather then assemble. **Must preserve:** the `parse_reply` contract — BOT_INSTRUCTIONS must carry the THOUGHT:/FACE:/ops text verbatim or downstream parsing changes. The golden test guards this.
- **`core/memory.py`** — `CuratedMemory.__init__` (85-86) just stores the root. `apply_memory_op` dispatch (92-106), `_apply_*` writers, `read_about/read_summary/read_collection/read_all_collections/read_directive` accessors. **Must preserve:** `DIRECTIVE.md` has NO write path anywhere (disjoint-writer invariant, AD-6) — do not add one. Core stays LLM-free (AD-1, import-linter) — `importlib.resources` is stdlib, safe.

### The binding AD-6 prompt order (design §3)
```
BOT_INSTRUCTIONS (system) → DIRECTIVE (authoritative) → IDENTITY → SOUL → USER → about → knowledge → summary → recent → recall → owner_message
```
Rationale: identity/soul/user are stable self-and-owner context, placed right after the owner's authoritative directive and before the volatile memory/recall layers, so persona shapes every reply. This ordering is also the cache-prefix shape Story 10.5 depends on — keep the persona block stable and first, volatile content last.

### KEY DECISION baked into this story (empty SOUL/IDENTITY/USER seeds)
The design (§10.1) requires a golden test: "assembled prompt with seed files **equals the prior hardcoded prompt** (no behavior change on day one)." If SOUL/IDENTITY/USER shipped with starter prose, they'd add new sections and break byte-parity. So they ship **empty** — present on disk (for 10.2 rewrite targets + 10.4 onboarding + bot awareness) but omitted from the prompt until populated. BOT_INSTRUCTIONS carries the verbatim system text. This is the clean reading of the design and defers persona *content* to onboarding (10.4), exactly where the design says SOUL/IDENTITY/USER get filled. If Elliot wants non-empty starter prose now, the golden test becomes "assembled == new expected" instead of "== prior" — flag before changing.

### Invariants that MUST hold (design §5)
- **AD-1 core LLM-free:** persona is data read by the worker; `core/memory.py` only seeds + reads files. `importlib.resources` is stdlib. import-linter must stay green.
- **AD-5 single-writer / AD-6 disjoint-writer:** this story adds NO new writers. Seed-on-absent only fills absent files (a one-time fill, not a competing writer). `DIRECTIVE.md` write-path absence preserved.
- **AD-3 fork = no accumulation:** files read fresh each fork; no new resident state.
- **Fail-soft (4.1 discipline):** every new read degrades + logs, never raises.

### Packaging gotcha
hatchling `[tool.hatch.build.targets.wheel] packages = ["shelldon"]` includes the package tree; non-`.py` files under it *should* ship, but **verify** with `unzip -l dist/*.whl | grep persona` after `uv build`. If they're excluded, add a `force-include` / `artifacts` entry to `pyproject.toml`. The Pi migration (Story 10.5) relies on these templates shipping, so getting packaging right here matters downstream.

### Project Structure Notes
- New subpackage `shelldon/persona/` sits beside `core/`, `worker/`, etc. — consistent with the existing top-level package layout. It holds only data (`.md`) + an empty `__init__.py`; no logic.
- No `contracts/` changes (deliberate — ops are 10.2).
- Test files extend the existing `tests/test_memory.py` and `tests/test_prompt_assembly.py` rather than adding new files, matching the per-module test convention.

### References
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#4 — Story 10.1 spec]
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#3 — Architecture: seam changes + prompt assembly order]
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#5 — Invariants preserved]
- [Source: shelldon/worker/prompt.py#45-95 — `SYSTEM_INSTRUCTION` to migrate then delete]
- [Source: shelldon/worker/prompt.py#112-147 — `assemble_prompt` order to extend]
- [Source: shelldon/core/memory.py#147-191 — `read_about`/accessor pattern + `_atomic_write_text`]
- [Source: shelldon/core/faces.py#168-188 — `FaceRegistry.load` seed-on-absent idiom to mirror]
- [Source: shelldon/core/runtime.py#311 + shelldon/worker/prompt.py#168 — both `CuratedMemory()` construction sites (boot + per-fork)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Full suite: `python -m pytest -q` → **782 passed, 0 failed, 3 skipped** (baseline was 780+2 pre-existing-assumption failures, both fixed — see Completion Notes).
- import-linter: `lint-imports` → **3 contracts KEPT, 0 broken** (AD-1 core-LLM-free holds; `importlib.resources` is stdlib).
- Packaging: `uv build --wheel` → all 4 persona files present in `shelldon/persona/` inside the wheel (BOT_INSTRUCTIONS.md 2264 bytes, SOUL/IDENTITY/USER 0 bytes, __init__.py). hatchling ships them automatically — no `force-include` needed.

### Completion Notes List

- **Persona moved to files, constant deleted (AC1/AC4/AC5).** `shelldon/persona/` holds the seed templates; `BOT_INSTRUCTIONS.md` is the byte-identical `SYSTEM_INSTRUCTION` text (copied via `cp` + `cmp`-verified to guarantee the golden test). The constant is gone from source (`grep` confirms only docstrings reference the name now).
- **Seed copy-if-absent (AC2/AC3).** `CuratedMemory._seed_persona()` runs in `__init__`, reads each template via `importlib.resources.files("shelldon.persona")`, writes absent files with the existing `_atomic_write_text`, skips present ones. Fail-soft (logs + swallows) so construction never raises at boot or per-fork.
- **Read accessors (AC4).** `read_instructions/read_soul/read_identity/read_user` mirror `read_about` exactly.
- **Assembly wiring (AC4/AC7).** `gather_context` returns `system`/`identity`/`soul`/`user`; `assemble_prompt` injects them in the binding order `system → directive → identity → soul → user → about → …`. New sections omit-if-empty, so day-one (empty SOUL/IDENTITY/USER) is byte-identical to the prior prompt — proven by `test_golden_day_one_no_op_equals_prior_hardcoded`.
- **DEVIATION from story note (AC6, improved).** The story task 4 suggested the *coarse* shared fail-soft handler. I made each persona read **independently** fail-soft via a `_safe_read` helper, because the coarse handler would let a corrupt SOUL.md (read early) also drop the system instruction + directive — violating AC6's "other sections still present". Cost: one tiny helper. Covered by `test_gather_corrupt_persona_degrades_only_its_section`.
- **Char-budget (AC4).** `_bounded_text` caps each persona section at `PERSONA_CHAR_BUDGET=8000` (seed is ~2.2KB), truncate-with-logged-warning — never silent.
- **`seed_instructions()` helper added to `prompt.py`** as the single DRY source of the canonical system text for tests/live-smokes (replaces the deleted constant they imported). Reads the repo template; this is NOT a hardcoded constant (AC5 satisfied — source of truth is the file).
- **Fixed 2 pre-existing tests** whose assumption "constructing CuratedMemory writes nothing" no longer holds now that init seeds persona files: `test_invalid_collection_rejected_without_writing` (now asserts the rejected op's target dir is absent) and `test_atomic_write_leaves_prior_about_on_crash` (now asserts no `.tmp` artifact survived). Both still verify their original intent.
- **Live smokes** (`test_turn_dream_live_smoke.py`) updated to use `seed_instructions()` — out of CI but import-clean so collection doesn't break the default suite.

### File List

- `shelldon/persona/__init__.py` (NEW — empty subpackage marker)
- `shelldon/persona/BOT_INSTRUCTIONS.md` (NEW — verbatim system instruction seed)
- `shelldon/persona/SOUL.md` (NEW — empty seed)
- `shelldon/persona/IDENTITY.md` (NEW — empty seed)
- `shelldon/persona/USER.md` (NEW — empty seed)
- `shelldon/core/memory.py` (UPDATE — seed-on-init + 4 read accessors)
- `shelldon/worker/prompt.py` (UPDATE — delete `SYSTEM_INSTRUCTION`, persona read/inject, `seed_instructions`/`_safe_read`/`_bounded_text`)
- `tests/test_memory.py` (UPDATE — persona seed/skip/idempotent/accessor tests; fix 2 pre-existing)
- `tests/test_prompt_assembly.py` (UPDATE — persona order, empty-omit, golden no-op, corrupt-degrade, seed-system tests)
- `tests/test_end_to_end_turn.py` (UPDATE — use `seed_instructions()` instead of the deleted constant)
- `tests/test_turn_dream_live_smoke.py` (UPDATE — use `seed_instructions()`)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (UPDATE — epic-10 in-progress, 10-1 review)

### Review Findings

- [x] [Review][Defer] Persona read accessors use `.read_text()` without `encoding="utf-8"` [shelldon/core/memory.py:192-210] — deferred, pre-existing. All read accessors in this file (`read_about`, `read_summary`, `read_directive`) use the same pattern; new accessors mirror them exactly per spec. No real risk on the Pi (UTF-8 locale). Trigger: encoding hygiene sweep across the whole file.

## Change Log

- 2026-06-25 — Implemented Story 10.1: persona-as-files foundation. Moved `SYSTEM_INSTRUCTION` into `shelldon/persona/` seed templates, seed copy-if-absent into the memory root on `CuratedMemory` init, read + inject persona into the prompt in the binding AD-6 order, deleted the constant. Day-one byte-parity proven by golden test. 782 passed / import-linter green / persona ships in wheel. (Opus 4.8)
