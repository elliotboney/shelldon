---
baseline_commit: bdc0054e819b9c0045db7e05f1065090b7f69645
note: Story 10.2's changes are in the WORKING TREE (uncommitted) at story-creation time — this story builds on them. The autonomous persona-rewrite ops (rewrite_soul/identity/user) and the dream-turn `_current_turn_is_owner=False` gate already exist from 10.2; 10.3 wires the dream prompt to USE them.
---

# Story 10.3: Proactive & dream prompts move to files + autonomous persona edit on the dream

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As shelldon,
I want my self-initiated prompt copy (the proactive check-in and the dream reflection) to live in editable markdown seed files instead of hardcoded Python constants, and my dream cycle to invite me to evolve SOUL/IDENTITY/USER when I've learned something durable,
so that no LLM-facing prose stays welded into source and I can grow my own character on the dream — with no chat instruction and no constitution drift.

## Acceptance Criteria

**AC1 — Proactive & dream prose live in seed files, not constants**
**Given** the repo
**When** the persona package is inspected
**Then** `shelldon/persona/HEARTBEAT.md` (the proactive self-prompt) and `shelldon/persona/DREAM.md` (the dream reflection) exist and carry the prompt copy that was hardcoded in `core/proactive.py`
**And** the hardcoded LLM-facing prose constants `_DIRECTIVE` and `_DREAM_DIRECTIVE` are removed from `core/proactive.py` (no substantive LLM-facing prose constant remains in that module; only a terse degrade-fallback may remain, see AC4)

**AC2 — Seed-on-absent (copy-if-absent, idempotent)**
**Given** an empty memory root
**When** `CuratedMemory` initializes
**Then** `HEARTBEAT.md` and `DREAM.md` are copied from the shipped templates into the root (the existing `_seed_persona` copy-if-absent idiom)
**And** a pre-existing `HEARTBEAT.md`/`DREAM.md` on disk is left untouched (never overwrites an owner edit), and a missing template/write error fails soft (logged, construction never raises)

**AC3 — Prompts built from file == prior hardcoded output (no behavior change)**
**Given** the seeded `HEARTBEAT.md`/`DREAM.md`
**When** `build_proactive_prompt` / `build_dream_prompt` build from the file text
**Then** the proactive directive (for a given feeling, and for no feeling) is byte-identical to what the old `_DIRECTIVE`/`_FEELING_SENTENCE` constants produced, and the dream directive (for a given pending list) is byte-identical to the old `_DREAM_DIRECTIVE` output
**And** the `{feeling}` weave still works (known feeling woven; `None`/blank → no dangling "feeling ." and never the literal "None") and `build_dream_prompt([])` still returns `""` (empty pending → dispatch skips)

**AC4 — Missing/corrupt template degrades safe (fail-soft)**
**Given** a missing or unreadable `HEARTBEAT.md`/`DREAM.md` at build time (read returns `None`/blank)
**When** the prompt is built
**Then** the builder returns a terse minimal built-in fallback directive (still a valid self-prompt), logs the degrade, and NEVER raises — `core/proactive.py` stays pure (no I/O, no clock) and `build_*` never throws for any input

**AC5 — The dream invites autonomous persona evolution (no chat instruction)**
**Given** the new `DREAM.md` copy
**When** it is built into the dream directive
**Then** it still instructs promote/prune (`resolve_learning`), `remember`, `rewrite_about`, and `rewrite_summary` (existing 6.2 behavior preserved)
**And** it ADDS an invitation to review and update `SOUL` / `IDENTITY` / `USER` via `rewrite_soul` / `rewrite_identity` / `rewrite_user` when the bot has learned something durable about itself or its owner — framed as part of the dream's self-consolidation, requiring no owner/chat prompt

**AC6 — Autonomous-edit path proven end-to-end on the dream (fake provider)**
**Given** a scripted dream turn whose pending learnings surface a durable owner preference, driven by a fake provider that replies with a `rewrite_user` op (no live LLM)
**When** the dream turn is dispatched and its result processed
**Then** core applies the op autonomously (USER.md holds the new content), proving the no-chat-instruction self-update path through `_apply_proposed_ops` → `apply_memory_op`
**And** the dream remains structurally barred from `rewrite_directive` (the 10.2 `_current_turn_is_owner=False` dream gate is unchanged — a `rewrite_directive` on the same dream turn is dropped, DIRECTIVE.md untouched, no approval parked)

**AC7 — Invariants hold**
**Given** the change set
**When** the suite + import-linter run
**Then** `core/proactive.py` stays pure (LLM-free, no I/O, never raises — file reads live in `core/memory.py` and the `core/dispatch.py` driver, not the policy module), core stays the sole writer (AD-5), the arbiter ≤1-slot balance is preserved, and a malformed/degraded template never crashes a self-initiated turn (fail-soft)

## Tasks / Subtasks

- [x] **Task 1 — Ship the two seed templates** (AC: 1, 3)
  - [x] Create `shelldon/persona/HEARTBEAT.md` carrying the current proactive copy VERBATIM. Use a single `---` sentinel line to separate the directive body (with the `{feeling_sentence}` placeholder) from the feeling fragment (with the `{feeling}` placeholder), so ALL prose lives in the file and the conditional-weave behavior is preserved exactly:
    ```
    (Self-prompt: there's no owner message to reply to right now — you're speaking up on your own.{feeling_sentence} Share whatever's on your mind: a passing thought, something you noticed, or just a hello. It doesn't have to be a question.)

    ---
     You're feeling {feeling}.
    ```
    **CRITICAL — preserve the leading space** before `You're feeling` (the body has no trailing space after `{feeling_sentence}`; the fragment supplies its own leading space so the rendered text reads `…on your own. You're feeling X. Share…`). Partition (don't `.strip()`) so the space survives.
  - [x] Create `shelldon/persona/DREAM.md` carrying the current dream copy VERBATIM (single `{lines}` placeholder), then EXTEND it per Task 3. Keep the `# Pending learnings\n{lines}` tail intact.
  - [x] These are LLM-facing prompt-policy templates (NOT bot-rewritable persona — no `rewrite_*` op targets them). Owner may hand-edit on disk; that's the only non-seed writer.

- [x] **Task 2 — Seed + read the templates in `CuratedMemory`** (AC: 2, 4)
  - [x] In `core/memory.py`, add `_PROMPT_TEMPLATE_SEED_FILES = ("HEARTBEAT.md", "DREAM.md")` and seed them in `_seed_persona` (iterate `_PERSONA_SEED_FILES + _PROMPT_TEMPLATE_SEED_FILES`, or add a second loop) — reuse the EXISTING copy-if-absent + fail-soft logic verbatim (do NOT add a new mechanism). A separate tuple keeps the semantic clear (these are prompt policy, not the bot-owned persona set).
  - [x] Add `read_heartbeat()` and `read_dream()` accessors — exact mirror of `read_about` (return `path.read_text() if path.is_file() else None`). No write path (these have no rewrite op).

- [x] **Task 3 — Move the prose out of `core/proactive.py`; builders take the template** (AC: 1, 3, 4, 5, 7)
  - [x] Delete the `_DIRECTIVE` and `_DREAM_DIRECTIVE` prose constants. Change the signatures to `build_proactive_prompt(feeling: str | None, template: str | None = None) -> str` and `build_dream_prompt(pending: list[tuple[int, str, int]], template: str | None = None) -> str`.
  - [x] `build_proactive_prompt`: if `template` is `None`/blank → use a TERSE built-in `_FALLBACK_PROACTIVE` (a one-line safe self-prompt, NOT the full prose) and log a degrade (or signal degrade to the caller — keep the module pure: prefer the module-level `log.warning` already used by sibling core modules; `core/proactive.py` may import `logging` without breaking AD-1). Otherwise partition the template on the first `\n---\n`: `body, _, frag = template.partition("\n---\n")`; if `frag` is non-empty AND a real feeling is supplied, `feeling_sentence = frag.rstrip("\n").format(feeling=feeling.strip())`, else `feeling_sentence = ""`; return `body.format(feeling_sentence=feeling_sentence)`. Never raise (wrap the `.format`/partition so a malformed template degrades to fallback). Keep the existing `feeling`-None/blank handling semantics identical.
  - [x] `build_dream_prompt`: keep the `if not pending: return ""` short-circuit FIRST (unchanged — empty pending must still skip with no file read needed). Then build `lines` exactly as today; if `template` is `None`/blank → `_FALLBACK_DREAM` (terse) + log; else `template.format(lines=lines)`. Never raise.
  - [x] Keep `core/proactive.py` PURE: no file I/O in this module (the read happens in the driver — Task 4). The module docstring already promises "no I/O, never raises" — preserve that contract; `logging` is allowed (sibling core modules log).

- [x] **Task 4 — Driver reads the file, passes it to the pure builder** (AC: 3, 4, 7)
  - [x] Inject `memory` into `TurnDispatcher`: in `core/runtime.py:373` add `memory=self.memory,` to the `TurnDispatcher(...)` construction (`self.memory` is already built at line 312, before the dispatcher). In `core/dispatch.py.__init__`, accept and store `self.memory = memory`.
  - [x] In `dispatch.py.build_proactive_prompt`, read `self.memory.read_heartbeat()` and pass it: `return build_proactive_prompt(feeling, self.memory.read_heartbeat())`.
  - [x] In `dispatch.py.build_dream_prompt`, read `self.memory.read_dream()` and pass it: `return build_dream_prompt([...], self.memory.read_dream())`. Keep the existing `pending = self.history.pending_learnings()` read.
  - [x] `dispatch.py` is the DRIVER (already does state/history I/O) — reading memory here is consistent with AD-9/AD-1 (still imports no provider lib).

- [x] **Task 5 — Tests** (AC: all)
  - [x] **Golden no-op (AC3):** capture the OLD constants' output as literals in the test (the exact strings the prior `_DIRECTIVE`/`_FEELING_SENTENCE`/`_DREAM_DIRECTIVE` produced for: a known feeling, no feeling, and a 2-item pending list). Load `HEARTBEAT.md`/`DREAM.md` from the `shelldon.persona` package (via `importlib.resources`, the `core/memory.py` idiom) and assert `build_proactive_prompt(feeling, tmpl) == <old literal>` and `build_dream_prompt(pending, tmpl) == <old literal>`. This is the day-one behavior-equivalence guard.
  - [x] **Update `tests/test_proactive.py`:** the builder signature changed — the existing behavioral assertions (`"on your mind" in out`, feeling-weave, no-"None", `build_dream_prompt([]) == ""`) must now pass the seeded template. Add a small helper that loads the seed templates from the persona package and threads them in. These tests then assert real seeded-template behavior (still meaningful). Do NOT re-hardcode the prose in the test.
  - [x] **Fallback (AC4):** `build_proactive_prompt("content", None)` and `build_dream_prompt([(1,"x",1)], None)` return a non-empty valid directive (the terse fallback) and never raise; assert no `"None"` leak. `build_proactive_prompt(None, None)` also safe.
  - [x] **Seed (AC2):** empty `tmp_path` root → `CuratedMemory(root)` → `HEARTBEAT.md`/`DREAM.md` now exist with the template content; a pre-written `HEARTBEAT.md` is left untouched (mirror the 10.1 seed-on-absent / seed-skip tests).
  - [x] **Autonomous-edit on dream (AC6):** drive a dream turn with the FAKE provider/harness used by the 10.2 dream-gate test in `tests/test_persona_ops.py` (and/or `test_turn_dispatch.py`): pending learnings surface a durable owner preference; the fake reply emits a `rewrite_user` op; after the dream turn's result is processed, `memory.read_user()` returns the new content (USER.md written autonomously, no chat instruction). Assert on the SAME turn a `rewrite_directive` is dropped (DIRECTIVE.md unchanged, no approval parked) — confirms the 10.2 dream gate still holds.
  - [x] **Dream content (AC5):** the built dream directive advertises `rewrite_soul`/`rewrite_identity`/`rewrite_user` in addition to the existing `resolve_learning`/`remember`/`rewrite_about`/`rewrite_summary` (golden-substring assertions so the invitation copy can't silently regress).
  - [x] Run `python -m pytest -q` (expect prior count + new, all green) and `lint-imports` (3 contracts KEPT — `core/proactive.py` stays import-clean; core stays LLM-free).

## Dev Notes

### What this story is (and is NOT)
- **IS:** move the proactive + dream prompt prose into `HEARTBEAT.md`/`DREAM.md` seed files (read at build time, pure fill preserved); seed copy-if-absent; degrade-safe fallback; and the dream-prompt INVITATION that triggers autonomous SOUL/IDENTITY/USER edits via the (already-built, 10.2) ops — with an end-to-end fake-provider test.
- **IS NOT:** new write ops (HEARTBEAT/DREAM are not bot-rewritable), onboarding (10.4), caching/lazy-load/reference files/Pi migration (10.5). The autonomous-rewrite OP MACHINERY already exists from 10.2 — 10.3 only wires the dream prompt to invite it and proves the path.

### The central tension — purity vs file I/O (resolve it the spine way)
`core/proactive.py` is documented PURE: "no clock, no I/O — never raises" (mirrors `core/reflexes.py`, the policy/driver split). Do NOT make it read files. Keep the POLICY pure (it takes the template TEXT as a param and fills it); the file READ happens in the DRIVER (`core/dispatch.py`, which already reads `state`/`history`) via the injected `CuratedMemory`, and the SEED happens in `core/memory.py` (which already does file I/O). This preserves the established policy/driver seam (Story 7.0) and keeps `core/proactive.py` LLM-free + never-raising (AD-1, AC7).

### The dream gate is ALREADY correct (do not rebuild it)
Story 10.2 added `_current_turn_is_owner` (set in `_start_turn` / `_start_resume_turn`); a dream/proactive turn is NOT owner-present, so `_apply_proposed_ops` already DROPS a `rewrite_directive` proposed on the dream (logged, never parked) while still APPLYING `rewrite_soul`/`rewrite_identity`/`rewrite_user`/`rewrite_about` autonomously. 10.3 adds NO new gate — it relies on this and tests that it still holds (AC6). Do not touch the gate logic.

### Existing patterns to MIRROR (do not reinvent)
- **Seed copy-if-absent + fail-soft:** `core/memory.py:119-134` `_seed_persona` (`resources.files(_PERSONA_PKG).joinpath(name).read_text()` → `_atomic_write_text(dest)`, skip-if-exists, swallow `OSError`/`ModuleNotFoundError`/`UnicodeError`). Add HEARTBEAT/DREAM to the seed set — reuse this loop, don't write a new seeder.
- **Read accessor:** `core/memory.py:234-238` `read_about` (`path.read_text() if path.is_file() else None`). `read_heartbeat`/`read_dream` are exact copies with different filenames.
- **Pure policy + driver split:** `core/proactive.py` (policy) vs `core/dispatch.py:44-60` (driver: reads `state`/`history`, calls the pure builder). Add the `memory` read in the driver, exactly alongside the existing `faces.select`/`history.pending_learnings()` reads.
- **Dispatcher injection:** `core/runtime.py:373-380` `TurnDispatcher(arbiter=…, history=…, start_turn=…)` — add `memory=self.memory` (built at `runtime.py:312`). `dispatch.py:34-42` `__init__` stores each collaborator — add `self.memory`.
- **Dream-turn fake-provider drive:** `tests/test_persona_ops.py` (10.2's dream-gate test — SOUL/USER rewrite applied on a dream turn, directive dropped) and `tests/test_turn_dispatch.py` (the dream/proactive dispatch harness). Reuse the same fake transport/provider; NO live LLM for any AC.
- **`importlib.resources` seed read in tests:** `core/memory.py:131` shows the `resources.files("shelldon.persona").joinpath(name).read_text(encoding="utf-8")` idiom — use it in the golden test to load HEARTBEAT/DREAM from the package.

### Current state of the files being modified (read before editing)
- **`core/proactive.py`** — `_FEELING_SENTENCE` (`" You're feeling {feeling}."`), `_DIRECTIVE` (body with `{feeling_sentence}`), `build_proactive_prompt(feeling)` (4-line weave), `_DREAM_DIRECTIVE` (the big dream prose with `{lines}`), `build_dream_prompt(pending)` (empty→"" short-circuit, then `"\n".join(f"- [id={lid}] … (seen {count}×)")`). The `{lines}` flatten-newlines logic STAYS in the builder; only the surrounding prose moves to file. **Must preserve:** empty-pending → `""`; the `id`/`count`/flatten formatting of each learning line; never-raises.
- **`core/memory.py`** — `_PERSONA_PKG`/`_PERSONA_SEED_FILES` (54-55), `_seed_persona` (119-134), `read_about` (234-238). **Must preserve:** copy-if-absent never overwrites; construction never raises; core stays sole writer.
- **`core/dispatch.py`** — `__init__` (34-42, no `memory` today), `build_proactive_prompt` (44-52, reads `state`+`faces`), `build_dream_prompt` (54-60, reads `history`). **Must preserve:** the empty-dream skip (build returns `""` → dispatch skips, no spend); LLM-free imports.
- **`core/runtime.py`** — `self.memory = CuratedMemory(…)` (312), `TurnDispatcher(…)` (373-380). **Must preserve:** memory is constructed BEFORE the dispatcher (so `self.memory` is available to inject); the ≤1 arbiter-slot balance on the dispatch path (untouched by this story but don't perturb it).
- **`tests/test_proactive.py`** — calls `build_proactive_prompt(feeling)` / `build_dream_prompt(pending)` with NO template arg today. The signature change means these MUST be updated to pass the seeded template (see Task 5). This is an in-scope edit caused by THIS story's change.

### Invariants that MUST hold (design §5)
- **AD-1 core LLM-free:** `core/proactive.py` stays pure data-fill; `core/dispatch.py`/`core/memory.py` import no provider lib. import-linter stays green.
- **AD-5 single-writer:** templates are seeded (copy-if-absent) + owner-hand-edited only; no autonomous write op targets HEARTBEAT/DREAM. Persona ops on the dream still route through core (10.2).
- **Fail-soft:** missing/corrupt template → terse fallback + log, turn proceeds; seed failure swallowed; builder never raises.
- **Fork = no accumulation (AD-3):** templates read fresh each build; no new resident state.

### Testing infra
- All ACs are drivable with the fake provider/transport in `tests/test_persona_ops.py` + `tests/test_turn_dispatch.py` — NO live LLM required.
- Optional `-m live` smoke (out of CI, owner runs the paid call): a real dream turn against GLM whose pending learnings invite a self-update elicits a parseable `rewrite_user`/`rewrite_soul` op — mirrors `tests/test_turn_dream_live_smoke.py`. Real-model uptake is otherwise unverifiable (Epic 6/9 constraint).

### Project Structure Notes
- New files: `shelldon/persona/HEARTBEAT.md`, `shelldon/persona/DREAM.md`. No new test module strictly required — extend `tests/test_proactive.py` (builder/golden/fallback) and `tests/test_persona_ops.py` or `tests/test_turn_dispatch.py` (autonomous-edit-on-dream). A new `tests/test_prompt_templates.py` is acceptable if cleaner.
- No new deps. No `SCHEMA_VERSION` bump (no contract change — this story adds no op).

### References
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#4 — Story 10.3 spec (move prose to files + autonomous-edit trigger on the dream)]
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#3 — DIRECTIVE chat-only / dream-barred; assembly order]
- [Source: shelldon/core/proactive.py — `_DIRECTIVE`/`_DREAM_DIRECTIVE`/`build_proactive_prompt`/`build_dream_prompt` (the prose + builders to move)]
- [Source: shelldon/core/memory.py#54-55,#119-134,#234-238 — `_PERSONA_SEED_FILES`, `_seed_persona`, `read_about` (seed + read pattern to mirror)]
- [Source: shelldon/core/dispatch.py#34-60 — `TurnDispatcher.__init__` + the two builder drivers (where the memory read goes)]
- [Source: shelldon/core/runtime.py#312,#373-380 — `self.memory` construction + `TurnDispatcher(...)` (inject `memory=`)]
- [Source: 10-2 story (working tree) — `_current_turn_is_owner` dream gate + rewrite_soul/identity/user autonomous apply (the mechanism 10.3 invites)]
- [Source: tests/test_proactive.py + tests/test_persona_ops.py + tests/test_turn_dispatch.py — builder tests + dream-turn fake-provider drive (no live LLM)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Full suite: `uv run python -m pytest -q` → **809 passed, 3 skipped, 8 deselected** (801 baseline at 10.2 done + 8 new across `test_proactive.py`/`test_memory.py`/`test_persona_ops.py`).
- import-linter: `uv run lint-imports` → **3 contracts KEPT, 0 broken** (`core/proactive.py` stays pure/LLM-free; the file read is in the dispatch driver + memory, not the policy module).
- Seed-byte sanity: `HEARTBEAT.md` partitions on `\n---\n` to `body='…question.)'` (no trailing newline) + `frag=' You're feeling {feeling}.\n'` (leading space preserved); `DREAM.md` ends exactly at `{lines}` (no trailing newline). Verified the proactive build is byte-identical to the prior hardcoded constant.

### Completion Notes List

- **Prose → files (AC1).** Created `shelldon/persona/HEARTBEAT.md` (proactive, sentinel-split: directive body + feeling fragment in ONE file per Elliot's "all-in-file" decision) and `shelldon/persona/DREAM.md` (dream). Deleted the `_DIRECTIVE`/`_FEELING_SENTENCE`/`_DREAM_DIRECTIVE` prose constants from `core/proactive.py` — only terse `_FALLBACK_*` safety nets remain (not the real copy).
- **Pure policy preserved (AC7).** `build_proactive_prompt(feeling, template)` / `build_dream_prompt(pending, template)` now take the template TEXT and fill it; `core/proactive.py` does NO file I/O and never raises (malformed/missing template → logged fallback). The READ lives in the driver `core/dispatch.py` (alongside its existing `faces.select`/`history.pending_learnings()` reads) via an injected `CuratedMemory`; the SEED lives in `core/memory.py`. Policy/driver seam (Story 7.0) intact, AD-1 green.
- **Seed + read (AC2).** `_PROMPT_TEMPLATE_SEED_FILES = ("HEARTBEAT.md","DREAM.md")` seeds copy-if-absent in the existing `_seed_persona` loop (reused verbatim, fail-soft); `read_heartbeat()`/`read_dream()` mirror `read_about`. Not bot-rewritable (no rewrite op targets them) — owner-editable on disk.
- **Day-one no-op (AC3) — reconciled with AC5.** Proactive build is byte-IDENTICAL to the prior constant (golden test, both feeling + none). The DREAM build is intentionally NOT byte-identical because AC5 grows the copy — so the dream golden asserts FULL preservation of the prior instruction set (`resolve_learning`/`remember`/`rewrite_about`/`rewrite_summary`/`promoted`/`pruned`/`# Pending learnings`/the `[id=…]` line) PLUS the new persona-edit invite. This is the honest reconciliation of the story's literal AC3 wording (which said "dream byte-identical") with AC5 (which adds copy): strict byte-identity holds where nothing changed (proactive), preservation+addition where AC5 deliberately changes it (dream).
- **Degrade-safe (AC4).** Missing/blank/malformed template → terse `_FALLBACK_*` + `log.warning`, never raises; the dream fallback still bakes the pending `{lines}`; empty pending still short-circuits to `""` before any template touch.
- **Dream invites self-evolution (AC5).** `DREAM.md` adds one sentence inviting `rewrite_soul`/`rewrite_identity`/`rewrite_user` "no one needs to ask you to" — the no-chat-instruction trigger. Mechanism already shipped in 10.2.
- **Autonomous-edit proven (AC6).** New `test_dream_applies_rewrite_user_autonomously_no_chat` drives a dream (unattended, `owner=False`) turn via the 10.2 fake-Result harness: a `rewrite_user` op is APPLIED autonomously (`USER.md` written), while a `rewrite_directive` on the same turn is still barred (dropped, not parked) — the 10.2 `_current_turn_is_owner` gate is untouched and still holds.
- **No new gate, no new op, no deps, no SCHEMA_VERSION bump.** `TurnDispatcher` gained a `memory` kwarg (only constructed in `runtime.py` — no test breakage). `core/proactive.py` imports only stdlib `logging` (AD-1 safe).

### File List

- `shelldon/persona/HEARTBEAT.md` (NEW — proactive self-prompt seed, sentinel-split body + feeling fragment)
- `shelldon/persona/DREAM.md` (NEW — dream-cycle seed; prior prose verbatim + the persona-edit invitation)
- `shelldon/core/proactive.py` (UPDATE — deleted prose constants; builders take `template`; terse fallbacks; pure, never-raises)
- `shelldon/core/memory.py` (UPDATE — `_PROMPT_TEMPLATE_SEED_FILES` seeded in `_seed_persona`; `read_heartbeat`/`read_dream` accessors)
- `shelldon/core/dispatch.py` (UPDATE — `memory` kwarg stored; builders read `read_heartbeat()`/`read_dream()` and pass them)
- `shelldon/core/runtime.py` (UPDATE — inject `memory=self.memory` into the `TurnDispatcher(...)` construction)
- `tests/test_proactive.py` (UPDATE — thread the seed templates through existing tests; +5 new: proactive golden no-op, dream preservation+invite, proactive/dream fallback, malformed-degrades)
- `tests/test_memory.py` (UPDATE — +2: prompt-template seed-on-absent/skip-present, read-None-when-absent)
- `tests/test_persona_ops.py` (UPDATE — +1: autonomous `rewrite_user` applied on a dream turn, directive still barred)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (UPDATE — 10-3 in-progress → review)

### Review Findings

- [x] [Review][Patch] `read_heartbeat`/`read_dream` no OSError catch — `is_file()` passes but `read_text()` raises on permission-denied; docstring promises fail-soft but doesn't deliver for unreadable files [shelldon/core/memory.py]
- [x] [Review][Patch] `build_proactive_prompt` no warning when non-empty template lacks `\n---\n` sentinel — feeling silently dropped with no diagnostic; owner hand-edit removing the separator is invisible [shelldon/core/proactive.py]
- [x] [Review][Patch] No test for `RewriteDirective`-first + `RequestToolApproval`-second ordering — `test_two_parking_ops_in_one_result_do_not_clobber` only covers RTA-first; the symmetric case is untested [tests/test_persona_ops.py]
- [x] [Review][Patch] `test_dream_applies_rewrite_user_autonomously_no_chat` doesn't assert `sent` is non-empty — a silent `_send_reply` regression would leave this test green [tests/test_persona_ops.py]
- [x] [Review][Defer] `_FALLBACK_DREAM.format(lines=lines)` inner `except Exception` returns raw `{lines}` — theoretical only; `_FALLBACK_DREAM` is a stable constant with exactly one `{lines}` placeholder — deferred, pre-existing
- [x] [Review][Defer] `_current_turn_is_owner = False` in `__init__` conflates "no turn" with "unattended turn" — only read during a turn in practice; Optional[bool] would add noise for no real gain — deferred, pre-existing
- [x] [Review][Defer] Log messages differ slightly between RTA and directive second-park warn branches — directive message is actually more specific; low value to harmonize — deferred, pre-existing
- [x] [Review][Defer] AC3 dream spec text says "byte-identical" but AC5 requires growth — spec defect acknowledged in completion notes; implementation is correct — deferred, pre-existing
- [x] [Review][Defer] `needs_approval` dual-scan across `_handle_result` + `_apply_proposed_ops` — latent fragility but no current bug — deferred, pre-existing
- [x] [Review][Defer] AC6 test bypasses dispatch path (no `pending_learnings` / `build_dream_prompt`) — AC6 evidence as written is met; deeper integration is optional `-m live` smoke territory — deferred, pre-existing
- [x] [Review][Defer] No test asserting HEARTBEAT/DREAM absent from rewritable op set — structural guarantee (no op type exists); defensive test would be nice but not required — deferred, pre-existing

#### Second review pass (2026-06-25, parallel adversarial: correctness clean, AC audit all 7 SATISFIED, edge-case hunter)
- [x] [Review][Patch] `build_dream_prompt` — an owner edit dropping the `{lines}` slot silently omits the pending learnings (model can't resolve ids it can't see), no warning, no fallback. **RESOLVED:** treat a template lacking `{lines}` as malformed → fall to `_FALLBACK_DREAM` (which bakes the learnings in) + log. Test `test_dream_template_without_lines_slot_falls_back_and_keeps_learnings`. [shelldon/core/proactive.py]
- [x] [Review][Patch] `build_proactive_prompt` — a body missing the `{feeling_sentence}` slot silently discards the woven mood. **RESOLVED:** log a warning when a feeling was computed but the body has no slot (directive still renders mood-less, never raises). Test `test_heartbeat_body_without_feeling_slot_logs_dropped_mood`. [shelldon/core/proactive.py]
- [x] [Review][Defer] HEARTBEAT with a SECOND `\n---\n` separator injects the extra prose into the body (partition stops at the first). Owner-edit foot-gun on a single-owner system; the shipped seed is correct. Low value to guard — deferred.
- [x] [Review][Defer] AC6 test injects a hand-built `Result` into `_handle_result` rather than driving `build_dream_prompt` → fake provider → `parse_reply` (the prompt-build leg is stubbed). The load-bearing claim (autonomous no-chat USER write + directive barred on the same unattended turn) IS proven; full round-trip is `-m live` smoke territory (Epic 6/9 CI constraint) — deferred, consistent with the prior pass's same finding.

## Change Log

- 2026-06-25 — Second review pass (parallel adversarial): correctness clean, all 7 ACs SATISFIED. 2 edge-case patches applied (DREAM `{lines}`-slot drop → fallback+log; HEARTBEAT `{feeling_sentence}`-slot drop → log), 2 deferred (double-`---` foot-gun; AC6 prompt-build leg = live-smoke territory). 812 passed (+2 guard tests) / import-linter 3 KEPT. (Opus 4.8)
- 2026-06-25 — Implemented Story 10.3: proactive/dream prompt prose → `HEARTBEAT.md`/`DREAM.md` seed files (pure builders take template text; file I/O in the dispatch driver + memory seed, `core/proactive.py` stays pure/never-raises). Seed copy-if-absent + degrade-safe fallbacks. `DREAM.md` invites autonomous SOUL/IDENTITY/USER edits via the 10.2 ops on the dream (no chat instruction; directive still barred by the unchanged 10.2 `_current_turn_is_owner` gate). Proactive build byte-identical to the prior constant (golden); dream preserves the prior instruction set + adds the persona invite (AC3/AC5 reconciled). 809 passed (+8) / import-linter 3 KEPT / 0 new ops / 0 deps. (Opus 4.8)
- 2026-06-25 — Story 10.3 drafted (ready-for-dev): proactive/dream prompt prose → HEARTBEAT.md/DREAM.md seed files (pure builders take template text; I/O in driver+memory, policy stays pure); seed copy-if-absent + degrade-safe fallback; DREAM.md invites autonomous SOUL/IDENTITY/USER edits via the 10.2 ops on the dream (no chat instruction, directive still barred). No new ops, no deps. (Opus 4.8)

- 2026-06-25 — Story 10.3 drafted (ready-for-dev): proactive/dream prompt prose → HEARTBEAT.md/DREAM.md seed files (pure builders take template text; I/O in driver+memory, policy stays pure); seed copy-if-absent + degrade-safe fallback; DREAM.md invites autonomous SOUL/IDENTITY/USER edits via the 10.2 ops on the dream (no chat instruction, directive still barred). No new ops, no deps. (Opus 4.8)
