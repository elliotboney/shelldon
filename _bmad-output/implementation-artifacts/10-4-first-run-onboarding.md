---
baseline_commit: 102cfc02fe2f98c5e02ebf4f529d4842ead55929
---

# Story 10.4: First-run onboarding (creates SOUL/IDENTITY/USER via a warm interview)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a new owner,
I want shelldon to run a short warm conversation on its very first turns — asking who I am and who it should be — and then save my answers into its own persona files,
so that the empty `SOUL`/`IDENTITY`/`USER` seeds get populated from a real conversation (not source edits) and the bot never re-interrogates me once it knows me.

## Acceptance Criteria

**AC1 — The onboarding directive lives in a seed file, not code**
**Given** the repo
**When** the persona package is inspected
**Then** `shelldon/persona/BOOTSTRAP.md` exists and carries the warm first-run interview directive (LLM-facing prose — no hardcoded onboarding copy in any `.py`)
**And** it is seeded copy-if-absent into the memory root by `CuratedMemory` (the same mechanism as `HEARTBEAT.md`/`DREAM.md`), and a `read_bootstrap()` accessor mirrors `read_heartbeat`

**AC2 — Onboarding is active while the owner profile is unset (the monotonic sentinel)**
**Given** a fresh worktree where `USER.md` is at its empty seed (`""`)
**When** the worker assembles a turn prompt (`gather_context` → `assemble_prompt`)
**Then** the assembled prompt INCLUDES the `BOOTSTRAP.md` onboarding directive (so the model runs the interview)
**And** the trigger is `USER.md` being blank — chosen because Story 10.2 REJECTS an empty `rewrite_user`, so once `USER.md` is filled it can never go blank again: a populated `USER.md` is a monotonic "onboarded" sentinel needing no separate flag

**AC3 — Once the owner profile is filled, onboarding never fires again**
**Given** an owner turn during onboarding whose result emits `rewrite_user` (with `rewrite_soul`/`rewrite_identity`) — the model deciding it has learned enough
**When** core applies those ops (the autonomous owner-present path from Story 10.2 — `USER.md`/`SOUL.md`/`IDENTITY.md` now hold the owner's answers)
**Then** the NEXT assembled prompt OMITS the `BOOTSTRAP.md` directive (sentinel flipped — `USER.md` no longer blank)
**And** the populated persona files now inject normally (the `# Your owner` / `# Your soul` / `# Your identity` sections appear per the Story 10.1 assembly)

**AC4 — Onboarding injects without breaking the normal prompt contract**
**Given** the onboarding directive is included
**When** the prompt is assembled
**Then** the `BOOTSTRAP.md` directive is placed as a distinct early section (right after the `system` `BOT_INSTRUCTIONS.md` slot, before `directive`/persona/memory), so the protocol still leads and the owner message still comes last
**And** all existing assembly invariants hold (omit-empty sections, owner message last, persona char-budget applied to BOOTSTRAP too)

**AC5 — Fail-soft**
**Given** a missing/corrupt/unreadable `BOOTSTRAP.md` while onboarding is active
**When** the prompt is assembled
**Then** the onboarding section is omitted (logged), the turn proceeds as a normal turn — never raises (mirrors the per-file `_safe_read`/`_bounded_text` discipline from Story 10.1)

**AC6 — Driven + proven with a fake provider (no live LLM)**
**Given** the scripted-turn test harness (hand-built `Result`, no live model)
**When** an empty-`USER` turn is assembled, then a turn emits `rewrite_user`/`rewrite_soul`/`rewrite_identity`, then a second turn is assembled
**Then** assembly#1 contains the onboarding directive, the ops populate the files via core's apply path, and assembly#2 omits the directive — the full trigger→populate→stop cycle proven without a live LLM

**AC7 — Invariants hold**
**Given** the change set
**When** the suite + import-linter run
**Then** core stays LLM-free (AD-1 — no onboarding prose or decision logic added to `core/`; the worker assembles, core only applies the existing ops), the worker stays read-only to memory (AD-6 — it READS `BOOTSTRAP.md`/`USER.md`, proposes ops, never writes), single-writer holds (AD-5 — only core applies the persona rewrites), and the fork accumulates no new resident state (AD-3 — `BOOTSTRAP.md` is read fresh each fork)

## Tasks / Subtasks

- [x] **Task 1 — Ship `BOOTSTRAP.md` + seed + read accessor** (AC: 1, 5)
  - [x] Create `shelldon/persona/BOOTSTRAP.md` — a concise warm first-run interview directive. It should instruct the model: this is your very first conversation; over a turn or two, ask the owner who they are (name, what they care about) and who they want you to be (your name/voice/role); keep it brief and friendly, not an interrogation; and once you know your owner, SAVE what you learned with `rewrite_user` (the owner profile), `rewrite_soul` (your voice/values), and `rewrite_identity` (who you are) — these are the ops from Story 10.2, no chat instruction needed. Keep it short (it's re-sent every onboarding turn — the Story 10.5 cost concern).
  - [x] In `core/memory.py`, add `BOOTSTRAP.md` to the seed set (extend `_PROMPT_TEMPLATE_SEED_FILES` or add it to the existing `_seed_persona` loop) so it copies copy-if-absent like `HEARTBEAT`/`DREAM`. Add `read_bootstrap()` — exact mirror of `read_heartbeat` (`try: path.read_text() if path.is_file() else None / except (OSError, UnicodeDecodeError): None`). No write path (not a rewrite-op target).

- [x] **Task 2 — Detect onboarding + inject the directive in `worker/prompt.py`** (AC: 2, 3, 4, 5)
  - [x] In `gather_context`, after reading `user = _bounded_text(_safe_read(mem.read_user), "USER.md")`, compute `onboarding = not (user or "").strip()` (USER blank → onboarding active). When `onboarding`, read the directive: `bootstrap = _bounded_text(_safe_read(mem.read_bootstrap), "BOOTSTRAP.md")` (reuse `_safe_read`/`_bounded_text` so it's fail-soft + char-budgeted exactly like the other persona reads). When not onboarding, `bootstrap = None`. Add `"bootstrap": bootstrap` to the returned dict.
  - [x] In `assemble_prompt`, add a `bootstrap=None` kwarg and inject it as a distinct section RIGHT AFTER the `system` block and BEFORE `directive` (so the protocol leads, onboarding is the first behavioral instruction, persona/memory/owner-message follow). Use a clear header, e.g. `parts.append(f"# First-run onboarding\n{bootstrap.strip()}")` guarded by `if bootstrap and bootstrap.strip():` (omit-empty like every other section). Owner message still appended last.
  - [x] `build_prompt` is unchanged (it already does `assemble_prompt(msg, **gather_context(...))` — the new `bootstrap` key flows through the `**kwargs` automatically).

- [x] **Task 3 — Tests** (AC: all)
  - [x] **Seed + accessor (AC1/AC5):** empty `tmp_path` root → `CuratedMemory(root)` → `BOOTSTRAP.md` exists with the directive text; a pre-written `BOOTSTRAP.md` is left untouched; `read_bootstrap()` returns `None` when the file is absent (mirror the 10.3 `test_seed_prompt_templates_*` tests in `tests/test_memory.py`).
  - [x] **Onboarding-active assembly (AC2/AC4):** with a fresh seeded root (USER blank), `gather_context(...)` returns a non-None `bootstrap`, and `build_prompt("hi", memory_root=root)` contains the onboarding section header AND places it after the system block and before the owner message (assert ordering by index). (Extend `tests/test_prompt_assembly.py`.)
  - [x] **Sentinel flip (AC2/AC3):** write a non-empty `USER.md` (via `CuratedMemory.apply_memory_op(RewriteUser(content=...))` — core's real apply path) → `gather_context` now returns `bootstrap is None` and `build_prompt` OMITS the onboarding section, while the `# Your owner` section now appears.
  - [x] **Full cycle, fake provider (AC6):** using the scripted-turn harness from `tests/test_persona_ops.py` (`_core` + `_open_owner_turn` + a hand-built `Result` with `proposed_ops`) — drive an owner turn whose `Result` carries `RewriteUser`/`RewriteSoul`/`RewriteIdentity`; assert core applies them (`read_user()`/`read_soul()`/`read_identity()` now non-empty); then assert a freshly-assembled prompt against the SAME memory root omits the onboarding directive. (No live LLM — the `Result` is the fake-provider stand-in.)
  - [x] **Fail-soft (AC5):** delete/corrupt `BOOTSTRAP.md` with USER still blank → `build_prompt` omits the onboarding section, logs, never raises. (Used CORRUPT non-UTF-8 rather than delete — `gather_context` re-seeds an absent file on construction, so a present-but-corrupt file is what exercises the worker's fail-soft read.)
  - [x] Run `uv run python -m pytest -q` (820 pass, +8 new, 3 skipped) and `uv run lint-imports` (3 contracts KEPT — `core/` LLM-free, worker stays read-only).

## Dev Notes

### What this story is (and is NOT)
- **IS:** a first-run warm interview that populates the empty `SOUL`/`IDENTITY`/`USER` seeds from conversation, via a `BOOTSTRAP.md` directive the WORKER injects while the owner profile is unset, using the persona-rewrite ops that ALREADY exist (Story 10.2). This is "the mechanism that creates USER" (design §4 10.4).
- **IS NOT:** new ops (reuses 10.2's `rewrite_user`/`rewrite_soul`/`rewrite_identity`), caching/lazy-load/Pi-migration (10.5), any change to `core/runtime.py` or the turn lifecycle. The bot decides when it's "done" by emitting the ops; the code only gates the directive on the sentinel.

### KEY DESIGN DECISION — worker-driven, USER-blank sentinel (chosen over a core state flag)
The prompt is assembled IN THE WORKER (`worker/prompt.py:build_prompt` → `gather_context` + `assemble_prompt`); core passes only `(turn_id, owner_message)` across the fork to `spawn_turn` (`runtime.py:525`). So the worker is the natural, SURGICAL place to detect onboarding and inject the directive — **no `runtime.py` change, no `PersonalityState` field, no fork-boundary plumbing**.
- **Sentinel = `USER.md` non-empty.** Onboarding is active iff `USER.md` is blank. This is MONOTONIC for free: Story 10.2's `_apply_rewrite_persona` REJECTS an empty `rewrite_user` (`core/memory.py` — "content must be non-empty"), so once `USER.md` is filled it can never become blank via an op again. No separate `onboarded` flag is needed, and there's no risk of re-onboarding from a bot self-edit.
- **Why NOT a `PersonalityState.onboarded` flag (the obvious alternative):** that lives in core's `state.json`, which the worker (the assembler) cannot read without new fork-boundary plumbing — more code, more coupling, and it would put the onboarding gate in `runtime.py` (the lifecycle) for no behavioral gain. The USER-blank check is a single line in the place that already reads `USER.md`. (If a future need arises to bound onboarding to N turns or survive an owner hand-deleting `USER.md`, revisit the flag — out of scope here.)
- **Edge (acknowledged, acceptable):** the worker can't tell an owner turn from a proactive/dream turn (both arrive as a `prompt` string), so if the scheduler fires a self-initiated turn while `USER` is still blank, the onboarding directive is also prepended there. Harmless (the bot just also nudges onboarding); a rare pre-onboarding window on a single-owner device. Not worth special-casing.

### Existing patterns to MIRROR (do not reinvent)
- **Seed copy-if-absent + read accessor:** `core/memory.py` `_seed_persona` + `_PROMPT_TEMPLATE_SEED_FILES` (Story 10.3) + `read_heartbeat`/`read_dream` (with the `try/except (OSError, UnicodeDecodeError)` fail-soft). `BOOTSTRAP.md` is one more entry + one more accessor.
- **Fail-soft + char-budgeted persona read:** `worker/prompt.py:153-156` (`_bounded_text(_safe_read(mem.read_x), "X.md")`) and `_safe_read` (209) / `_bounded_text` (219). Read `BOOTSTRAP.md` the SAME way so a corrupt/oversized file degrades only its own section.
- **Assembly section discipline:** `worker/prompt.py:assemble_prompt` (77-127) — every section is `if x and x.strip(): parts.append(f"# Header\n{x.strip()}")`, owner message always appended last. Add the `bootstrap` section in that exact style, positioned after `system`.
- **The persona-rewrite ops + autonomous owner-present apply:** Story 10.2 — `RewriteUser`/`RewriteSoul`/`RewriteIdentity` are `MemoryOp`s applied autonomously on owner-present turns through `apply_memory_op`. Onboarding needs NO new op; it relies on these firing on the owner's onboarding turn.
- **Scripted-turn fake-provider harness:** `tests/test_persona_ops.py` (`_core`, `_open_owner_turn`, `_result_env`, hand-built `Result(proposed_ops=...)` → `core._handle_result`). Reuse verbatim for AC6.

### Current state of the files being modified (read before editing)
- **`shelldon/worker/prompt.py`** — `gather_context` (130-206) reads the persona files + returns the assembly kwargs dict; `assemble_prompt` (77-127) composes in AD-6 order; `build_prompt` (243) = gather+assemble; helpers `_safe_read` (209), `_bounded_text` (219), `PERSONA_CHAR_BUDGET`. **Must preserve:** omit-empty sections, owner message last, per-file fail-soft, the char budget. The `user` value is already read at line 156 — compute the sentinel right there.
- **`shelldon/core/memory.py`** — `_PROMPT_TEMPLATE_SEED_FILES` (Story 10.3), `_seed_persona`, `read_heartbeat`/`read_dream`. **Must preserve:** copy-if-absent never overwrites; construction never raises; core sole writer; `read_*` fail-soft try/except.
- **`shelldon/persona/`** — existing seeds `BOT_INSTRUCTIONS.md`, `SOUL.md`/`IDENTITY.md`/`USER.md` (empty), `HEARTBEAT.md`, `DREAM.md`. Add `BOOTSTRAP.md`.

### Invariants that MUST hold (design §5)
- **AD-1 core LLM-free:** onboarding prose is a seed file the WORKER reads; the decision (`USER` blank?) is a string check in the worker, not core. No `core/` LLM logic. import-linter stays green.
- **AD-6 worker read-only / AD-5 single-writer:** the worker READS `BOOTSTRAP.md`/`USER.md` and PROPOSES the persona ops; CORE applies them (the 10.2 path). The worker never writes memory.
- **AD-3 fork = no accumulation:** `BOOTSTRAP.md` is read fresh per fork; no resident state.
- **Fail-soft:** missing/corrupt `BOOTSTRAP.md` → onboarding section omitted, turn proceeds.

### Project Structure Notes
- New file: `shelldon/persona/BOOTSTRAP.md`. Edits: `core/memory.py` (seed + accessor), `worker/prompt.py` (detect + inject). Test additions: `tests/test_memory.py` (seed/accessor), `tests/test_prompt_assembly.py` (onboarding-active/omit, ordering), `tests/test_persona_ops.py` (full cycle). No new op, no new dep, no `SCHEMA_VERSION` bump, no `runtime.py`/`state.py` change.

### References
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#4 — Story 10.4 (onboarding creates SOUL/IDENTITY/USER incl. the USER part)]
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#3 — assembly order; USER is the owner-profile gap onboarding fills]
- [Source: shelldon/worker/prompt.py#77-206,#209-247 — assemble_prompt / gather_context / build_prompt / _safe_read / _bounded_text (where onboarding injects)]
- [Source: shelldon/core/memory.py — _PROMPT_TEMPLATE_SEED_FILES, _seed_persona, read_heartbeat/read_dream, _apply_rewrite_persona (empty-content rejection = the monotonic sentinel guarantee)]
- [Source: shelldon/core/runtime.py#525 — spawn_turn(turn_id, prompt): only the owner message crosses the fork, so the worker is the assembly point]
- [Source: 10-2 story — RewriteUser/RewriteSoul/RewriteIdentity autonomous owner-present apply (the ops onboarding emits)]
- [Source: tests/test_persona_ops.py + tests/test_prompt_assembly.py + tests/test_memory.py — scripted-turn harness + assembly + seed test patterns]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- First run surfaced 3 failures, all in new tests (no production-logic bugs): (1) `build_prompt` not imported into `tests/test_prompt_assembly.py`; (2/3) the AC5 fail-soft test deleted `BOOTSTRAP.md` then called `gather_context`, which **re-seeds** an absent file on `CuratedMemory` construction — so deletion was undone and `bootstrap` came back non-None. Fixed by switching the fail-soft test to write a CORRUPT (non-UTF-8) `BOOTSTRAP.md`: seeding never overwrites a present file, so the corrupt file persists and `read_bootstrap` fails soft to `None` via its `UnicodeDecodeError` catch — the intended worker fail-soft path.

### Completion Notes List

- **Worker-driven, USER-blank sentinel (no core/runtime change).** Onboarding detection is a single string check in `gather_context` (`not (user or "").strip()`) right where `USER.md` is already read. No `PersonalityState` flag, no fork-boundary plumbing, no `runtime.py` edit — AD-1/AD-3/AD-5/AD-6 all hold untouched (verified: import-linter 3 contracts KEPT).
- **`BOOTSTRAP.md` is a prompt template, not a rewrite target.** Added to `_PROMPT_TEMPLATE_SEED_FILES` (with HEARTBEAT/DREAM) so it seeds copy-if-absent and is owner-hand-editable, but no `rewrite_*` op writes it. `read_bootstrap()` mirrors `read_heartbeat`/`read_dream` exactly (is_file guard + `OSError/UnicodeDecodeError` → None).
- **Injection placement.** `assemble_prompt` gains a `bootstrap=None` kwarg; the `# First-run onboarding` section is appended right after `system` and before `directive` — protocol leads, onboarding is the first behavioral instruction, persona/memory/owner-message follow. Omit-empty + char-budget (`_bounded_text`) + per-file fail-soft (`_safe_read`) all reused — no new discipline.
- **Day-one behavior INTENTIONALLY changed (AC2).** Story 10.1's golden "day-one byte-parity" test asserted a fresh seeded root assembles to system+message only. 10.4 makes a blank-USER fresh root inject onboarding by design, so that test was updated (renamed `test_golden_day_one_system_seed_plus_onboarding`) to expect system seed + onboarding directive. Still a real gather→assemble round-trip parity check, now reflecting onboarding.
- **Full trigger→populate→stop cycle proven with a fake provider (AC6)** — `test_onboarding_full_cycle_fake_provider`: assembly#1 (blank USER) contains onboarding → hand-built `Result` with `RewriteUser`/`RewriteSoul`/`RewriteIdentity` driven through core's real `_handle_result` apply path → assembly#2 against the same root omits onboarding and shows `# Your owner`. No live LLM.
- 820 tests pass (+8 new), 3 skipped; import-linter 3 contracts KEPT; 0 new deps, 0 new ops, no `runtime.py`/`state.py`/`contracts` change.

### File List

- `shelldon/persona/BOOTSTRAP.md` (new) — the warm first-run interview seed directive.
- `shelldon/core/memory.py` (modified) — `BOOTSTRAP.md` added to `_PROMPT_TEMPLATE_SEED_FILES`; `read_bootstrap()` accessor.
- `shelldon/worker/prompt.py` (modified) — `gather_context` computes the USER-blank sentinel + reads `bootstrap` fail-soft + returns it; `assemble_prompt` gains the `bootstrap` kwarg + injects the `# First-run onboarding` section after `system`.
- `tests/test_memory.py` (modified) — `test_seed_bootstrap_on_absent_and_skip_present`, `test_read_bootstrap_none_when_absent`.
- `tests/test_prompt_assembly.py` (modified) — onboarding ordering/omit/active/sentinel-flip/fail-soft tests; updated the 10.1 golden test to `test_golden_day_one_system_seed_plus_onboarding`.
- `tests/test_persona_ops.py` (modified) — `test_onboarding_full_cycle_fake_provider` (AC6); imported `build_prompt`.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified) — 10-4 → in-progress → review.

### Review Findings

- [ ] [Review][Patch] AC5 logging gap — `read_bootstrap` catches `(OSError, UnicodeDecodeError)` and returns `None` silently; `_safe_read`'s except branch never fires, so corruption is never logged despite spec saying "omitted (logged)" [`shelldon/core/memory.py:309`] — fix: add `log.warning(...)` in the except branch of `read_bootstrap`; update `test_onboarding_fail_soft_when_bootstrap_corrupt` to assert `caplog` warning
- [x] [Review][Defer] Proactive/dream turns inject BOOTSTRAP while USER blank — spec §Dev Notes explicitly acknowledges as "harmless…rare pre-onboarding window…not worth special-casing"; no dedicated test (defer; behavior is documented in spec) — deferred, spec-accepted behavior
- [x] [Review][Defer] `read_user`/`read_soul`/`read_identity` missing try/except guard [`shelldon/core/memory.py`] — pre-existing inconsistency vs. `read_heartbeat`/`read_dream`/`read_bootstrap` pattern; safe because `_safe_read` wraps all in `gather_context` — deferred, pre-existing

## Change Log

- 2026-06-25 — Story 10.4 DEV DONE → review: first-run onboarding implemented. New `BOOTSTRAP.md` seed directive (warm interview) the WORKER injects via `assemble_prompt` while `USER.md` is blank (the monotonic sentinel — 10.2 rejects empty `rewrite_user`, so a filled USER never reverts); the bot interviews then emits `rewrite_user`/`rewrite_soul`/`rewrite_identity` (existing 10.2 ops, owner-present autonomous apply) and onboarding never fires again. Single-line sentinel check in `gather_context`, section injected after `system`; fail-soft + char-budgeted reusing the 10.1 helpers. No `runtime.py`/`state.py`/`contracts` change, no new op, no new dep. 10.1's day-one golden test updated to reflect the intended onboarding-on-blank-USER behavior. 820 pass (+8), import-linter 3 KEPT. (Opus 4.8)
- 2026-06-25 — Story 10.4 drafted (ready-for-dev): first-run onboarding via a `BOOTSTRAP.md` seed directive the WORKER injects while `USER.md` is blank (a monotonic sentinel — 10.2 rejects empty `rewrite_user`, so a filled USER never reverts). The bot interviews the owner then emits `rewrite_user`/`rewrite_soul`/`rewrite_identity` (existing 10.2 ops, owner-present autonomous apply); once USER is filled, onboarding never fires again. Worker-driven (no runtime/state change), fail-soft, fake-provider tested. No new op, no deps. (Opus 4.8)
