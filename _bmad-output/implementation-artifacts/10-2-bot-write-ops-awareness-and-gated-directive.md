---
baseline_commit: bdc0054e819b9c0045db7e05f1065090b7f69645
---

# Story 10.2: Bot-writable persona via memory-ops + awareness + gated directive

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As shelldon,
I want to evolve my own soul/identity/owner-profile/instructions at runtime through core's single-writer op path, and to propose changes to my owner's directive for approval,
so that my character can grow from what I learn — autonomously for my own files, owner-gated for the constitution — without anyone editing source.

## Acceptance Criteria

**AC1 — Autonomous persona rewrite ops**
**Given** the contract vocabulary
**When** the worker proposes `rewrite_soul` / `rewrite_identity` / `rewrite_user` / `rewrite_instructions` in its `\`\`\`ops` block
**Then** each decodes to a frozen tagged `MemoryOp` (mirrors `RewriteAbout`), routes through `CuratedMemory.apply_memory_op`, and writes its target file (`SOUL.md` / `IDENTITY.md` / `USER.md` / `BOT_INSTRUCTIONS.md`) atomically (temp + fsync + `os.replace`)
**And** an empty/blank `content` is rejected without touching disk (mirrors `_apply_rewrite_about`)

**AC2 — `rewrite_instructions` validate-on-apply guardrail**
**Given** a `rewrite_instructions` op whose new content drops a required protocol marker (the `THOUGHT:` directive, the `FACE:` directive, or the `\`\`\`ops` fence instruction)
**When** core applies it
**Then** the rewrite is **rejected** (logged, no-op — the prior `BOT_INSTRUCTIONS.md` is left intact), so the bot can re-voice its character freely but cannot delete the contract tokens `parse_reply` depends on
**And** a rewrite that keeps all required markers is applied normally

**AC3 — The bot is AWARE of its self-knowledge files**
**Given** the seeded `BOT_INSTRUCTIONS.md`
**When** it is assembled into a prompt
**Then** it contains a "Your self-knowledge files" section that (a) names each file and what it is (`SOUL` = voice/values, `IDENTITY` = who/hardware/mission, `USER` = what you know about your owner, `about` = your running self-summary) and (b) states the bot MAY rewrite them via the ops **with no chat instruction required** when it learns something durable
**And** it advertises the previously-unadvertised `rewrite_about` op (closing the latent gap where the model never knew it existed)

**AC4 — `rewrite_directive` is bot-proposable but owner-approval-gated (never autonomous)**
**Given** a `rewrite_directive` op proposed in the ops block
**When** core processes the turn's proposed ops
**Then** it does **NOT** apply autonomously — it is intercepted, an approval is parked (reusing the Story 9.3 plumbing) keyed by the turn id, and an Approve/Deny request is sent to the owner
**And** `rewrite_directive` is **NOT** a member of the `MemoryOp` autonomous-apply union — there is no code path that applies it without an owner approval

**AC5 — Approve applies in core; Deny skips (decision 2026-06-25)**
**Given** a parked `rewrite_directive` approval
**When** the owner taps **Approve**
**Then** core applies `CuratedMemory._apply_rewrite_directive(content)` **directly** (core is sole writer, AD-5 — no worker resume), `read_directive()` returns the new content, and the owner gets a confirmation
**When** the owner taps **Deny** (or the approval expires/unknown)
**Then** `DIRECTIVE.md` is unchanged and the owner gets a "left it as-is" note

**AC6 — Directive edits are chat-only, never in the unattended dream**
**Given** the dream/proactive (unattended) turn path
**When** a `rewrite_directive` op is proposed on such a turn
**Then** it is dropped (logged), never parked — the constitution can only be changed when the owner is present to approve (no drift while you're not looking). SOUL/IDENTITY/USER/about autonomous rewrites are still allowed on the dream.

**AC7 — Invariants hold**
**Given** the change set
**When** the suite + import-linter run
**Then** core stays LLM-free (AD-1), every persona/directive write goes through core (AD-5), `DIRECTIVE.md` has no autonomous write path, atomic-write/crash-safety holds, and a malformed op is logged+skipped never crashing the turn (fail-soft)

## Tasks / Subtasks

- [x] **Task 1 — Contracts: the four autonomous persona ops + the gated directive op** (AC: 1, 4, 7)
  - [x] In `shelldon/contracts/__init__.py`, add four frozen tagged structs mirroring `RewriteAbout` (single `content: str`, `frozen=True`, `forbid_unknown_fields=True`): `RewriteSoul` (tag `rewrite_soul`), `RewriteIdentity` (tag `rewrite_identity`), `RewriteUser` (tag `rewrite_user`), `RewriteInstructions` (tag `rewrite_instructions`).
  - [x] Add `RewriteDirective` (tag `rewrite_directive`, single `content: str`) — but keep it **OUT** of the `MemoryOp` union (it must never autonomously apply, AC4).
  - [x] Extend `MemoryOp` union: `Remember | RewriteAbout | LogEpisode | RewriteSummary | RewriteSoul | RewriteIdentity | RewriteUser | RewriteInstructions` (the 4 autonomous persona ops join; `RewriteDirective` does NOT).
  - [x] Extend `ProposedOp` union to include ALL five new ops (so `parse_reply`'s `_OPS_DECODER = msgspec.json.Decoder(list[ProposedOp])` can decode them from the ops block) — `RewriteDirective` IS in `ProposedOp` (proposable) even though it is NOT in `MemoryOp` (not autonomously applicable).
  - [x] Add all five to `__all__`.

- [x] **Task 2 — `CuratedMemory`: apply paths + the guardrail** (AC: 1, 2, 5, 7)
  - [x] Add `_apply_rewrite_soul/_apply_rewrite_identity/_apply_rewrite_user` — exact copies of `_apply_rewrite_about` (reject empty `content`, atomic write to `SOUL.md`/`IDENTITY.md`/`USER.md`).
  - [x] Add `_apply_rewrite_instructions` with the **validate-on-apply guardrail** (AC2): reject empty content AND reject if the new content is missing any required protocol marker. Required markers (substring checks): `"THOUGHT:"`, `"FACE:"`, and the ops-fence token `` "```ops" ``. On reject: `raise ValueError(...)` (the caller logs + no-ops, leaving the prior file). On pass: atomic write to `BOT_INSTRUCTIONS.md`.
  - [x] Add `_apply_rewrite_directive(content)` — atomic write to `DIRECTIVE.md`, reject empty content. **CRITICAL:** this method must be reachable ONLY from the core approval branch (Task 4), NEVER from `apply_memory_op` dispatch. Do NOT add a `RewriteDirective` branch to `apply_memory_op`.
  - [x] Wire the 4 autonomous ops into `apply_memory_op`'s `isinstance` dispatch (the existing if/elif chain). `RewriteDirective` is deliberately absent — if it somehow reaches `apply_memory_op`, the existing `else: raise ValueError("unknown memory-op …")` rejects it (defense in depth).
  - [x] Add a `read_directive` writer-free note is N/A — `read_directive` already exists (10.1). No new read accessors needed (10.1 added read_instructions/soul/identity/user).

- [x] **Task 3 — Awareness section in the seed `BOT_INSTRUCTIONS.md`** (AC: 3)
  - [x] Append a "Your self-knowledge files" section to `shelldon/persona/BOT_INSTRUCTIONS.md` that names SOUL/IDENTITY/USER/about, explains each, states the bot may rewrite them via `rewrite_soul`/`rewrite_identity`/`rewrite_user`/`rewrite_about` with NO chat instruction required when it learns something durable, and mentions `rewrite_directive` requires owner approval. Keep it concise (the persona prefix is re-sent every turn — Story 10.5 cost concern).
  - [x] This changes the seed template. The 10.1 golden test (`test_golden_day_one_no_op_equals_prior_hardcoded`) reads `seed_instructions()` dynamically on BOTH sides, so it stays green — confirm it still passes (it asserts assembly consistency, not a frozen literal). Do NOT re-introduce a hardcoded copy of the old text.
  - [x] Add a golden-STRING test asserting the assembled `BOT_INSTRUCTIONS.md` advertises every rewrite op name (`rewrite_soul`, `rewrite_identity`, `rewrite_user`, `rewrite_about`, `rewrite_instructions`) so the awareness copy can't silently regress.

- [x] **Task 4 — Core: gate `rewrite_directive` through the 9.3 approval plumbing** (AC: 4, 5, 6)
  - [x] In `runtime.py` `_apply_proposed_ops`, add a branch for `RewriteDirective` (BEFORE the `else: apply_memory_op`): instead of applying, park an approval and request owner confirmation. Reuse the 9.3 park: `self.history.park_approval(self._current_turn_id, blob, now)` where `blob` encodes the directive content (a distinct shape from the `(messages, call)` RISKY blob — see Task 4 sub-decision below). Then send `self._send_reply("Update your directive to: …? ", approval_turn_id=self._current_turn_id)` so the transport renders Approve/Deny.
  - [x] **Parked-blob shape:** the 9.3 RISKY approval parks `msgpack((messages, call))`; the 9.4 promotion parks a tool name. A directive rewrite is a THIRD kind. Pick the lowest-friction encoding that `_handle_approval_decision` can disambiguate. Recommended: park a `ToolCall(id=…, name="rewrite_directive", args={"content": content})` via the EXISTING `(messages, call)` blob shape with `messages=()` — then `_handle_approval_decision` checks `call.name == "rewrite_directive"` to branch to the core-apply path instead of `spawn_resume`. This reuses `take_approval`/`park_approval`/expiry verbatim with ZERO schema change. (Alternative: a dedicated `park_directive` table — more code; only do this if the shared blob proves awkward.)
  - [x] In `_handle_approval_decision`, after the 9.4 promotion check and after `take_approval` + decode, add: `if call.name == "rewrite_directive":` → on `approved` apply `self.memory._apply_rewrite_directive(call.args["content"])` (guarded; on success reply "Directive updated.", on ValueError reply a soft failure) and on `not approved` reply "Okay, left your directive as-is." — then `return` WITHOUT `_start_resume_turn` (no worker resume — core applied it). The arbiter slot reserved by `submit("[tool approval]")` must be released on this path (it never starts a turn) — call `self.arbiter.reset()` (or restructure so the directive branch runs before the `submit`). **Verify the slot is balanced** (a leaked slot wedges all later turns — see the 9.3/dispatch release discipline).
  - [x] **AC6 — dream gate:** a `rewrite_directive` must NOT be parked on an unattended turn. The turn that produced the ops knows if it was owner-initiated vs scheduler-driven. Determine the cleanest signal: `_apply_proposed_ops` runs for both owner turns and dream turns. Gate on whether the current turn is owner-present. Check how proactive/dream turns are distinguished (`record_owner_text` / the synthetic owner marker in `dispatch.py` / a flag on the turn) and drop+log `RewriteDirective` when not owner-present. If no clean in-scope signal exists, add a minimal one (a `self._current_turn_is_owner` flag set in `_start_turn` vs the dispatch path) — keep it surgical.

- [x] **Task 5 — Tests** (AC: all)
  - [x] **Round-trip apply (AC1):** for each of soul/identity/user/instructions — a `\`\`\`ops` block string → `parse_reply` → core `apply_memory_op` → the target file holds the new content; empty content rejected without writing.
  - [x] **Guardrail (AC2):** a `rewrite_instructions` dropping `FACE:` (and one dropping `\`\`\`ops`) is rejected — `BOT_INSTRUCTIONS.md` unchanged, logged; a valid re-voice (keeps all markers) is applied.
  - [x] **Crash-safety (AC7):** an interrupted `os.replace` on a persona rewrite leaves the prior file intact + no stray temp (mirror `test_atomic_write_leaves_prior_about_on_crash`).
  - [x] **Awareness (AC3):** golden-string assertion that the assembled BOT_INSTRUCTIONS advertises all rewrite ops incl. `rewrite_about`.
  - [x] **Directive gate (AC4/AC5):** drive the FULL park→approve and park→deny flows with the FAKE transport/harness from `tests/test_risky_approval.py` (NO live LLM): a proposed `rewrite_directive` does NOT apply immediately (DIRECTIVE.md unchanged, an approval is parked + an approval-tagged outbound sent); Approve → `read_directive()` returns the new content; Deny → unchanged. Assert it never routes through `apply_memory_op` (e.g. a `rewrite_directive` passed directly to `apply_memory_op` raises).
  - [x] **Dream gate (AC6):** a `rewrite_directive` proposed on a dream/unattended turn is dropped (DIRECTIVE.md unchanged, no approval parked); a SOUL/USER rewrite on the same dream turn IS applied (proves only directive is barred, not all persona).
  - [x] **Slot balance (AC5):** after an Approve and after a Deny of a directive, the arbiter is idle (no leaked slot) — a subsequent owner turn still runs.
  - [x] **[Optional, `-m live`] Real-model elicitation smoke:** mirroring `tests/test_turn_dream_live_smoke.py` — a turn whose owner message strongly invites a self-update (e.g. "from now on, always be more concise with me") elicits a parseable persona-rewrite op (`rewrite_user`/`rewrite_soul`/`rewrite_about`) from the live GLM model. Opt-in, network-gated, out of CI. Real-model uptake is otherwise unverifiable (Epic 6/9 constraint). Owner runs the paid call; the mechanism is proven by the fake-provider tests above.

## Dev Notes

### What this story is (and is NOT)
- **IS:** the four autonomous persona-rewrite ops (mirror `rewrite_about`), the `rewrite_instructions` parse-guardrail, the BOT_INSTRUCTIONS awareness section (incl. advertising the latent `rewrite_about`), and the owner-approval-gated `rewrite_directive`.
- **IS NOT:** moving the proactive/dream prompts to files (10.3), the autonomous-edit-on-dream TRIGGER copy (10.3), onboarding (10.4), caching (10.5). This story adds the *capability + gate*; 10.3 wires the dream to USE it.

### RESOLVED design decisions (Elliot, 2026-06-25) — these override the design doc's literal wording
The Epic 10 design said `rewrite_directive` "rides 9.3 verbatim → a resumed turn applies the rewrite." That conflicts with **AD-5 (the worker cannot write memory)** — the 9.3 resume path spawns a worker that *executes* the approved tool, but a directive write must happen in core. Elliot chose:
1. **Apply mechanism = core applies on Approve.** A new branch in `_handle_approval_decision` calls `_apply_rewrite_directive` directly — NO worker resume. Reuses ALL 9.3 plumbing (park / Telegram keyboard / `take_approval` / expiry).
2. **Propose mechanism = ops-block op.** `RewriteDirective` is a `\`\`\`ops` op (same modality as `rewrite_soul`/etc.), intercepted in `_apply_proposed_ops` and parked instead of applied. It is in `ProposedOp` (proposable) but NOT in `MemoryOp` (not autonomously applicable).

This keeps all persona rewrites in ONE modality (the ops block) and honors AD-5 (core sole writer, single authority on the directive).

### Existing patterns to MIRROR (do not reinvent)
- **Op struct shape:** `contracts/__init__.py:114-117` `RewriteAbout` — frozen, tagged, `forbid_unknown_fields`, single `content: str`. Your 5 new ops are exact copies with different tags.
- **Apply method:** `core/memory.py` `_apply_rewrite_about` (reject empty → `_atomic_write_text(self._root / "about.md", op.content)`). Soul/identity/user are byte-identical with different filenames; instructions adds the guardrail; directive is the same but only callable from the approval branch.
- **Dispatch chain:** `core/memory.py` `apply_memory_op` if/elif on `isinstance` — add the 4 autonomous ops; the `else: raise ValueError` already rejects anything not in `MemoryOp` (so a stray `RewriteDirective` is rejected there — defense in depth).
- **Ops decode union:** `worker/worker.py:83` `_OPS_DECODER = msgspec.json.Decoder(list[ProposedOp])` — adding ops to `ProposedOp` is what makes them parseable from the reply. No worker change beyond the union (parse_reply is generic).
- **Approval park + decision:** `runtime.py:1057-1066` (`RequestToolApproval` → `park_approval`) and `runtime.py:577-617` (`_handle_approval_decision`: 9.4 promotion branch, then `take_approval` + decode `(messages, call)` + `_start_resume_turn`). Your directive branch slots in alongside these — park like 9.3, branch on `call.name == "rewrite_directive"` in the decision handler to apply-in-core instead of resuming.
- **Slot discipline:** `runtime.py:616` `self.arbiter.submit("[tool approval]")` reserves the ≤1 slot for a resume turn. The directive branch does NOT start a turn, so it must release the slot (`arbiter.reset()`), OR run the directive branch BEFORE `submit`. See `dispatch.py:106-110` for the release-before-return discipline.
- **Guardrail markers:** `worker/worker.py:108-109` (`_THOUGHT_RE`/`_FACE_RE` parse `THOUGHT:`/`FACE:` lines) and `worker/worker.py:78` (`_OPS_BLOCK_RE` parses the `\`\`\`ops` fence). Your `_apply_rewrite_instructions` must require the content still contains `"THOUGHT:"`, `"FACE:"`, and `` "```ops" ``.

### Current state of the files being modified (read before editing)
- **`contracts/__init__.py`** — `RewriteAbout` (114), `MemoryOp` union (140), `ProposedOp` union (289), `__all__` (503-536). `ProposedOp` is assigned AFTER `Message`/`RequestToolApproval` because it references them; your new ops are simple (no forward refs) so they can be defined near `RewriteAbout` and just JOIN the two unions.
- **`core/memory.py`** — `apply_memory_op` (92-106), `_apply_rewrite_about` (108-111), `_atomic_write_text` (48-65), `read_directive` (185-191, read-only, has no writer — preserve that for the AUTONOMOUS path; the approval branch is the only writer). **Must preserve:** `DIRECTIVE.md` has no write path in `apply_memory_op` dispatch (AD-6 disjoint-writer); the new `_apply_rewrite_directive` is reached only from core's approval branch.
- **`runtime.py`** — `_apply_proposed_ops` (1022-1070), `_handle_approval_decision` (577-617), `_start_resume_turn` (619-641), `_handle_result` (643+). **Must preserve:** the ≤1 arbiter slot balance (every `submit` paired with a `complete`/`reset`); the existing RISKY-tool resume path (don't break `RequestToolApproval`/`ProposeTool`).
- **`shelldon/persona/BOT_INSTRUCTIONS.md`** — the verbatim seed from 10.1. Appending the awareness section is the only edit; keep the existing protocol copy intact (the guardrail + parse_reply depend on it).

### Invariants that MUST hold (design §5)
- **AD-1 core LLM-free:** new ops are data; `core/memory.py` only validates + writes. import-linter stays green.
- **AD-5 single-writer / AD-6 disjoint-writer:** SOUL/IDENTITY/USER/BOT_INSTRUCTIONS = bot-owned, core applies autonomously. `DIRECTIVE` = owner-authoritative; core writes it ONLY on owner-Approve. No unapproved directive write can land. Single-*authority* preserved even as single-*writer* relaxes.
- **Atomic / crash-safe:** all `_apply_rewrite_*` use `_atomic_write_text`.
- **Fail-soft:** a malformed op / rejected guardrail / corrupt parked blob logs + skips, never crashes the turn.

### Testing infra
- Drive the directive gate with the fake transport + harness in `tests/test_risky_approval.py` (the 9.3 approve/deny pattern) and `tests/test_selfcode_flow.py` (the 9.4 promotion approve/deny). NO live LLM needed for any AC.
- A live LLM (GLM via Z.ai, broker env) IS available if you want the optional elicitation smoke (Task 5 last item) — opt-in, `-m live`, out of CI; owner runs the paid call.

### Project Structure Notes
- No new files (extends `contracts/`, `core/memory.py`, `runtime.py`, the persona seed, and the existing test modules). Test additions extend `tests/test_proposed_ops.py` / `tests/test_memory.py` / `tests/test_risky_approval.py` per the per-module convention; a new `tests/test_persona_ops.py` is acceptable if cleaner.

### References
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#4 — Story 10.2 spec]
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#3 — central tension + DIRECTIVE gate]
- [Source: shelldon/contracts/__init__.py#114-140,#285-289 — RewriteAbout, MemoryOp/ProposedOp unions]
- [Source: shelldon/core/memory.py#92-191 — apply_memory_op dispatch + _apply_rewrite_about + read_directive]
- [Source: shelldon/core/runtime.py#577-641 — _handle_approval_decision + _start_resume_turn (9.3 plumbing to reuse)]
- [Source: shelldon/core/runtime.py#1022-1070 — _apply_proposed_ops (where RewriteDirective is intercepted)]
- [Source: shelldon/worker/worker.py#78-109,#147-174 — ops-block parse + THOUGHT/FACE markers for the guardrail]
- [Source: tests/test_risky_approval.py + tests/test_selfcode_flow.py — approve/deny drive pattern (no live LLM)]
- [Source: 10-1 story — persona files seeded + read_instructions/soul/identity/user accessors already exist]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Full suite: `python -m pytest -q` → **799 passed, 0 failed, 3 skipped** (782 baseline + 17 new in `test_persona_ops.py`).
- import-linter: `lint-imports` → **3 contracts KEPT, 0 broken** (core stays LLM-free).
- Sanity: ops-block JSON round-trips through `ProposedOp`; `RewriteDirective` is NOT a `MemoryOp` (autonomous apply rejects it); `rewrite_instructions` guardrail rejects a marker-dropping rewrite.

### Completion Notes List

- **Four autonomous persona ops (AC1).** `RewriteSoul/Identity/User/Instructions` added to `contracts/` (mirror `RewriteAbout`), joined `MemoryOp` + `ProposedOp`. `CuratedMemory` gained `_apply_rewrite_persona` (shared for soul/identity/user) + dispatch branches. Soul/identity/user are byte-for-byte the `rewrite_about` pattern (reject empty, atomic temp+rename).
- **Instructions guardrail (AC2).** `_apply_rewrite_instructions` rejects a rewrite missing any of `_REQUIRED_INSTRUCTION_MARKERS = ("THOUGHT:", "FACE:", "```ops")` — the bot can re-voice but can't break `parse_reply`. The pristine repo seed remains the recovery source.
- **Awareness (AC3).** Appended a "Your self-knowledge files" section to `shelldon/persona/BOT_INSTRUCTIONS.md` naming SOUL/IDENTITY/USER/about + advertising every rewrite op (incl. the latent `rewrite_about`) and noting directive needs approval. The 10.1 golden test stayed green (reads `seed_instructions()` dynamically). A new golden-string test pins the awareness copy.
- **Gated directive (AC4/AC5/AC6) — per the 2026-06-25 decisions.** `RewriteDirective` is in `ProposedOp` but NOT `MemoryOp`. `_apply_proposed_ops` intercepts it: dropped on unattended turns (AC6), else parked via the SAME 9.3 plumbing — encoded as a `(messages=(), ToolCall(name="rewrite_directive", args={content}))` blob (ZERO schema change, reuses `park_approval`/`take_approval`/expiry). `_handle_result` tags the reply for the Approve/Deny surface only when owner-present. `_handle_approval_decision` branches on `call.name == "rewrite_directive"` BEFORE the resume `submit` → applies `_apply_rewrite_directive` directly in core (no worker resume — honors AD-5), or skips on deny. No arbiter slot reserved on this path (verified idle after, no leak).
- **Owner-present signal (AC6).** Added `self._current_turn_is_owner`, set in `_start_turn` (`record_owner_text is None`) and `_start_resume_turn` (True). The dream/proactive path passes a synthetic marker → not owner → directive proposals dropped.
- **Invariants (AC7).** `_apply_rewrite_directive` is reachable ONLY from the core approval branch; `apply_memory_op`'s `else` rejects a stray `RewriteDirective` (defense in depth). Atomic writes throughout; malformed ops logged+skipped. import-linter green.
- **No new deps.** No `SCHEMA_VERSION` bump (the new ops are additive tagged union members; the directive blob reuses the existing `(messages, call)` shape).

### File List

- `shelldon/contracts/__init__.py` (UPDATE — 5 new ops, MemoryOp/ProposedOp unions, `__all__`)
- `shelldon/core/memory.py` (UPDATE — `_apply_rewrite_persona`/`_apply_rewrite_instructions`/`_apply_rewrite_directive`, dispatch branches, `_REQUIRED_INSTRUCTION_MARKERS`)
- `shelldon/core/runtime.py` (UPDATE — `RewriteDirective` import, `_current_turn_is_owner` flag, directive park in `_apply_proposed_ops`, approval-surface tagging in `_handle_result`, core-apply branch in `_handle_approval_decision`)
- `shelldon/persona/BOT_INSTRUCTIONS.md` (UPDATE — "Your self-knowledge files" awareness section)
- `shelldon/worker/worker.py` (UPDATE — review fix: anchor the ops-block closing fence to line-start so a nested ` ```ops ` in `rewrite_instructions` content can't close the block early)
- `tests/test_persona_ops.py` (NEW — 19 tests: persona-op round-trip incl. instructions, guardrail, crash-safety, awareness, directive gate park/approve/deny/dream-drop/slot-balance, two-parking-ops collision regression)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (UPDATE — 10-2 in-progress → review)

### Review Findings

- [x] [Review][Patch] park_approval key collision: RequestToolApproval + RewriteDirective (or multiple RewriteDirective) in one Result both call `park_approval(self._current_turn_id, ...)` — second overwrites first in sqlite (INSERT OR REPLACE), silently losing the first blob; owner approves the wrong thing with no indication the other was dropped. [shelldon/core/runtime.py:_apply_proposed_ops] Fix: add mutual-exclusion guard at the top of `_apply_proposed_ops` (or inline) — if both types are present, process only the first, log and skip extras; mirrors the ProposeTool guard pattern (line 728). **RESOLVED:** added a `parked_approval` flag in `_apply_proposed_ops`; the first parking op (RequestToolApproval or RewriteDirective) parks, any subsequent one is logged + skipped. Regression test `test_two_parking_ops_in_one_result_do_not_clobber` (RTA+directive in one Result → RTA blob survives, directive not applied).
- [x] [Review][Patch] approve test never asserts confirmation text — **RESOLVED:** `test_directive_approve_applies_in_core_no_resume` now asserts `"directive" in sent[0].lower()`.
- [x] [Review][Patch] deny test never asserts "left as-is" reply text — **RESOLVED:** `test_directive_deny_leaves_unchanged` now asserts `"left" in sent[0].lower()`.
- [x] [Review][Patch] rewrite_instructions parse→apply roundtrip not tested via parse_reply — **RESOLVED + uncovered a real wire bug.** Adding the round-trip exposed that `rewrite_instructions` could NEVER transit the ops block: its guardrail-valid content contains a literal ` ```ops ` fence, and the non-greedy `_OPS_BLOCK_RE` closed the outer block at that INNER fence (fence nesting) → malformed JSON → op silently dropped. Fix in `shelldon/worker/worker.py`: require the closing fence at line-start (`\n` before ` ``` `). Valid JSON escapes every real newline inside a string value, so an embedded fence is always mid-line and can't match — only the true closing fence does; multi-block support unchanged. New test `test_rewrite_instructions_roundtrip_parse_to_apply` proves instructions now round-trips parse→apply. This makes AC1 genuinely true for all four autonomous ops (it was silently broken for instructions before).
- [x] [Review][Defer] guardrail checks opening ```ops fence token only, not closing — a content with ```ops but no closing fence satisfies the check yet produces a malformed ops section. Spec explicitly says substring checks for `"```ops"`; working as designed. Pre-existing design choice.
- [x] [Review][Defer] proactive-unattended directive drop has no explicit test by name — AC6 says "dream/proactive"; test only names "dream". Same `_current_turn_is_owner = False` code path handles both; implementation correct. Low-value additional coverage.

## Change Log

- 2026-06-25 — Implemented Story 10.2: bot-writable persona ops + instructions guardrail + awareness + owner-gated directive. 4 autonomous rewrite ops (soul/identity/user/instructions, mirror rewrite_about), `rewrite_instructions` validate-on-apply guardrail, BOT_INSTRUCTIONS awareness section, and `rewrite_directive` gated through the 9.3 approval plumbing (ops-block op, parked, core-applies-on-approve, no worker resume, dropped on dream turns). 799 passed / import-linter green / 0 new deps. (Opus 4.8)
- 2026-06-25 — Addressed code review: 4 patches resolved, 2 deferred acknowledged. (1) park_approval collision guard (`parked_approval` flag — only the first parking op parks; +regression test). (2/3) approve/deny tests now assert the confirmation/denial reply text. (4) Added the instructions parse→apply round-trip test, which uncovered + fixed a real wire bug: the ops-block regex closed at a NESTED ` ```ops ` inside `rewrite_instructions` content — fixed by anchoring the closing fence to line-start (`shelldon/worker/worker.py`), making AC1 genuinely true for instructions. 801 passed / import-linter green. (Opus 4.8)
