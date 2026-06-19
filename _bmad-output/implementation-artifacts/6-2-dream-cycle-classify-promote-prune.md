---
baseline_commit: 66225e3
---

# Story 6.2: Dream cycle — classify, promote, prune

Status: done

<!-- Final feature story of Epic 6 (Dreaming & Learning). The consumer of 6.1's pending learnings. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to periodically reflect on what it captured and keep what matters,
so that recurring, high-value learnings become durable memory and the rest is cleared.

**Why now / what it unblocks:** This is the **second half of Epic 6** and the **consumer of 6.1**. 6.1 made the pet capture raw `pending` learnings to sqlite on the hot path; 6.2 is the **dream cycle** that periodically reviews them — promoting the durable/high-value ones into curated markdown, pruning the rest, and keeping a running conversation summary so context stays bounded. It is **CAP-11** ("the pet improves over time"): a learning captured today demonstrably shapes a reply tomorrow. The dream is **not a new subsystem** (AD-15) — it is a **scheduled introspective worker turn** that reuses the fork-server, broker, and arbiter exactly like a normal turn, and rides Story 5.4's `prompt_builder` turn-job seam + Story 5.2's cost weight + the 5.2/5.3 credit/battery gates. After 6.2, the daily-driver + autonomy + memory + learning loop is complete; only Epic 7 (optional extensibility/embodiment) remains.

**The elegant reuse (read first).** The dream is a **proactive-turn variant** — a `turn` Job with a heavier `cost`, a different `prompt_builder`, and a `history_owner_text` marker (all 5.4 seams). Core's `_build_dream_prompt` reads the `pending` learnings (core owns the sqlite store) and **bakes them into the dream directive** (exactly as 5.4's `_build_proactive_prompt` bakes mood) — the worker forwards it, the LLM classifies, and proposes ops. The worker's brief user-facing note ("💤 tidied my thoughts") rides the normal turn lifecycle to the owner; the ops apply via the unchanged 4.5 propose→apply path. **No new turn-lifecycle code** (the Epic 5 retro flagged `runtime.py` coupling — keep it minimal).

## Acceptance Criteria

### AC1 — The dream classifies pending learnings, promotes durable ones to markdown, prunes the rest

**Given** the dream cycle as a scheduled introspective worker turn (a 5.1 scheduler job, within the 5.2 budget + 5.3 battery rules)
**When** it runs
**Then** the LLM classifies the `pending` learnings and **promotes** durable/high-value ones (judged by **impact + recurrence, not a rigid count**) into curated markdown (`about.md`/`facts/`) **via memory-ops**, and **prunes** the rest — all as **proposed ops on its `Result`** that **core (sole writer, AD-5) applies**.

- The dream is an **`Idle`-cadence `turn` Job** (owner decision: ~6h owner silence) registered in `Core.__init__` like the 5.4 proactive job, with **`cost=3`** (a dream counts as 3 against the 12/day budget — the weight 5.2 built for) and a **`history_owner_text="(shelldon dreamed)"`** marker. It rides the **unchanged** 5.2/5.3 dispatch gates (arbiter-idle + cooldown + daily budget + battery backoff; `essential=False`).
- Core's `_build_dream_prompt` (the job's `prompt_builder`) reads the `pending` learnings (a new **core read** on `HistoryStore`, ordered by `recurrence_count` desc — impact first) and bakes them, **each tagged with its `id`**, into the dream directive instructing the LLM to keep the durable ones and let the rest go. **If there are no pending learnings, the builder returns empty → the dispatch skips it** (the 5.4 promptless-skip guard — no spend, no fork).
- **Promotion** reuses the existing 4.2 memory-ops (`remember` → `facts/`, `rewrite_about` → `about.md`). **Marking a learning resolved** is a **new op** `resolve_learning(id, status)` (`promoted`/`pruned`) the worker proposes and core applies to the sqlite row — a **soft transition** (status only, NOT a `DELETE`), so a learning the dream pruned but that **re-recurs** resets to `pending` (6.1 behavior) and gets another chance. Pruning = `resolve_learning(id, "pruned")`. An invalid/unknown/non-pending `id` is rejected without side effects (the per-op guard).

### AC2 — A promoted learning demonstrably reflects in a later reply (CAP-11)

**Given** a learning promoted in a dream cycle (written to `facts/`/`about.md`)
**When** a later, related turn runs
**Then** the reply demonstrably reflects it — because the 4.4 prompt assembly already injects `about.md` + FTS5 recall over history into every turn's prompt, a promoted fact reaches a later turn's context. The **mechanism** is proven deterministically (a promote op applied → the promoted content appears in a later assembled prompt); the LLM's actual classification judgment is unverifiable without a live LLM (note the limitation).

### AC3 — The dream also keeps a running conversation summary (light scope)

**Given** the dream cycle
**When** it runs
**Then** it **also consolidates recent conversation history into a running summary** so context stays bounded — proposed as a **new `rewrite_summary(content)` memory-op** that core writes to `memory/summary.md`, which the **4.4 prompt assembly injects into later turns** (bounded context). **LIGHT scope only**: no ERRORS/FEATURE_REQUESTS taxonomy, no CLAUDE.md/skill extraction, no history deletion/compaction beyond writing the summary doc.

### Out of scope (explicit — later stories)

- **Routing sensitive learnings to the broker-gated `vault/`** (the AD-15 clause) — **deferred to a follow-on** (owner decision). There is **no vault SURFACING path yet** (4.4 deferred reading `vault/` back into a prompt), so writing sensitive learnings to a vault nothing can read would be half a feature. 6.2 promotes to the readable markdown tree + prunes; the sensitive→vault route lands when vault surfacing exists. Note this gap in Dev Notes — do NOT build a write-only vault path.
- **A context-pressure trigger** (dream when pending count crosses a threshold) — 6.2 uses the **idle** trigger; a count-gated trigger is a later refinement (the scheduler has no count-gate today).
- **Real-LLM classification quality / prompt tuning** — no live-LLM lane; 6.2 tests the **mechanism** (read pending → bake → propose ops → apply → CAP-11 reflection) with synthetic dream `Result`s, exactly as the 4.5 apply tests construct `Result`s directly.
- **History compaction/deletion** — 6.2 writes a summary doc; it does NOT delete or truncate the `messages` table (that's a heavier consolidation; light scope).
- **A new turn lifecycle for the dream** — the dream rides the **normal** `_start_turn`/`_handle_result` path (a brief owner-facing note + the ops); reuse it verbatim (5.4 precedent). Do NOT add a "silent turn" mode.

## Tasks / Subtasks

- [x] **Task 1 — Contracts: the resolve + summary ops (`contracts/__init__.py`) (AC1, AC3)**
  - [x] `ResolveLearning(msgspec.Struct, frozen=True, tag="resolve_learning", forbid_unknown_fields=True)`: `id: int`, `status: Literal["promoted", "pruned"]`. A **sqlite** op (like `CaptureLearning`) → joins **`ProposedOp`** (NOT `MemoryOp`). Docstring: the worker references a pending learning by the `id` core baked into the dream prompt; core applies the soft status transition.
  - [x] `RewriteSummary(msgspec.Struct, frozen=True, tag="rewrite_summary", forbid_unknown_fields=True)`: `content: str`. A **markdown** op (like `RewriteAbout`) → joins **`MemoryOp`** (so it routes through `apply_memory_op`). Add both to `__all__`. **No `SCHEMA_VERSION` bump** (additive union variants — AD-13).
- [x] **Task 2 — sqlite: read pending + resolve (`core/history.py`) (AC1)**
  - [x] `HistoryStore.pending_learnings(limit: int = 50) -> list[sqlite3.Row]`: the `status='pending'` rows ordered by `recurrence_count DESC, id ASC` (impact-first, bounded). A **core read** (the dream's `prompt_builder` runs in core — no worker/read-only change needed; the 6.1 deferred "worker read path" is unneeded under this design).
  - [x] `HistoryStore.resolve_learning(id: int, status: str) -> None`: one transaction — `UPDATE learnings SET status = ? WHERE id = ? AND status = 'pending'` (only transitions a still-`pending` row; the CHECK constraint already forbids a bad status value). A non-existent / already-resolved `id` is a 0-row no-op (logged). Soft transition only — never `DELETE`.
- [x] **Task 3 — markdown: the running summary (`core/memory.py`) (AC3)**
  - [x] `CuratedMemory.apply_memory_op` gains a `RewriteSummary` branch → `_apply_rewrite_summary` (reject empty content; `_atomic_write_text(self._root / "summary.md", op.content)` — the existing atomic idiom). `read_summary() -> str | None` (mirrors `read_about`). `summary.md` is bot-owned (core sole writer); never a `DIRECTIVE.md` target.
- [x] **Task 4 — prompt: inject the summary; dream op vocabulary (`worker/prompt.py`) (AC2, AC3)**
  - [x] `gather_context`/`assemble_prompt` read + inject `summary.md` (via `CuratedMemory.read_summary`) as a bounded "# Conversation so far" section, placed after `# About you` and before `# Recent conversation` (AD-6 order: broad durable context before the raw recent window). Fail-soft like the other reads (a missing/locked summary degrades, never raises).
  - [x] Add a brief `SYSTEM_INSTRUCTION` mention of the dream ops (`resolve_learning`, `rewrite_summary`) so the model can emit them — light, in the existing ops-fence format; note the real-model effect is unverifiable (no live LLM).
- [x] **Task 5 — runtime: register the dream job + build its prompt + route the op (`core/runtime.py`) (AC1)**
  - [x] Add `DEFAULT_DREAM_IDLE_INTERVAL = 21600.0` (6h; injectable) + a `DREAM_OWNER_MARKER = "(shelldon dreamed)"`. Register the dream job in `Core.__init__` after the proactive job: `Job("dream", Idle(self.dream_idle_interval), CostTier.TURN, prompt_builder=self._build_dream_prompt, history_owner_text=DREAM_OWNER_MARKER, cost=3)`.
  - [x] `_build_dream_prompt(self) -> str`: read `self.history.pending_learnings()`; if empty, return `""` (→ the 5.4 `_resolve_job_prompt` skip — no dream when nothing's pending). Else bake a directive: a brief framing + the pending learnings each as `[id=N] <observation> (seen N×)`, instructing the LLM to (a) promote the durable/recurring ones via `remember`/`rewrite_about` AND `resolve_learning(id, "promoted")`, (b) `resolve_learning(id, "pruned")` the rest, (c) `rewrite_summary(...)` a short running summary, then reply with a brief note. Pure-ish (reads sqlite, no await — atomic in the admit section, like 5.4).
  - [x] In `_apply_proposed_ops`, add an `isinstance(op, ResolveLearning)` branch → `self.history.resolve_learning(op.id, op.status)` (before the `apply_memory_op` fallback; `RewriteSummary` falls through to `apply_memory_op` like the other markdown ops). Import the new ops. Keep the per-op guard.
  - [x] **Two Idle turn jobs now exist** (proactive 1h, dream 6h). Note the interaction in a comment: each fires once per idle stretch on its own period; a cold-start while already >6h idle could make both due in one tick → the arbiter admits one and defers the other (the deferred one re-proposes next idle stretch). Acceptable; do not add cross-job coordination.
- [x] **Task 6 — Tests (AC1, AC2, AC3)**
  - [x] `contracts` round-trip: a `Result` carrying `ResolveLearning` and `RewriteSummary` encodes/decodes (msgpack) and `parse_reply` decodes both ops blocks; a bad `status` / unknown field is a decode error (whole-reject).
  - [x] `tests/test_history.py` (extend): `pending_learnings` returns only `pending` rows, ordered by recurrence desc, bounded; `resolve_learning` transitions a pending row to promoted/pruned and is a no-op on a non-pending/absent id; a pruned row that re-captures (6.1) resets to pending.
  - [x] `tests/test_memory.py` (extend): `rewrite_summary` writes `summary.md` atomically; empty content rejected; `read_summary` round-trips; `DIRECTIVE.md` still unreachable.
  - [x] `tests/test_prompt_assembly.py` (extend): an assembled prompt injects `summary.md` in the right order; missing summary degrades (no section, no raise).
  - [x] Routing/integration (`tests/test_proposed_ops.py` + a dream suite): a synthetic dream `Result` with `[remember + resolve_learning(promoted) + resolve_learning(pruned) + rewrite_summary]` applies each to its writer (fact written, learnings transitioned, summary written) and the markdown/sqlite end states are correct; an invalid `resolve_learning` id is skipped, turn survives. **CAP-11 (AC2):** capture a learning → apply a dream that promotes it to `facts/` → a later turn's assembled prompt (4.4) contains the promoted fact (deterministic, synthetic ops — no live LLM). **Dream dispatch:** with pending learnings, `_build_dream_prompt` is non-empty and the dream dispatches (cost 3 spent); with none, it returns "" and the dream is skipped (no spend).
- [x] **Task 7 — Soak + full-suite + contracts**
  - [x] Soak (`-m soak`) green/unchanged — the dream job is a resident turn job parked with the scheduler (like the proactive job); no new db file (`summary.md` rides the existing memory root; learnings ride the existing `history.db`) → **no conftest change** (call it out). Full `pytest` green; both import-linter contracts **KEPT**. Apply `dev-loop-checklist.md` (incl. the Epic 5 input-edge sub-list).

### Review Findings

- [x] [Review][Patch] Dream directive offered `remember`→`facts/`, which 4.4 never injects — a model picking it writes durable content that silently never shapes replies. **FIXED**: the directive now steers to `rewrite_about` only (the surfaced doc); comment notes to reinstate `facts/` when 4.4 injects it. [`shelldon/core/runtime.py:_build_dream_prompt`]
- [x] [Review][Patch] `log.warning` on every normal empty-dream skip would spam prod every cadence. **FIXED**: an empty *builder* return is an intentional skip → `log.debug` in `_resolve_job_prompt` (a static promptless turn job stays `warning` — that IS a misconfig). [`shelldon/core/runtime.py:_resolve_job_prompt`]
- [x] [Review][Patch] No `scheduler.tick()` integration test for the dream cadence. **FIXED**: +`test_dream_fires_via_scheduler_tick_on_its_idle_cadence` (dream_idle_interval=1.0, proactive pushed out, pending learning seeded, long-ago `last_interaction` → dream fires via the real tick, cost 3). [`tests/test_turn_dispatch.py`]
- [x] [Review][Patch] `test_no_memory_op_writes_directive` omitted `RewriteSummary`. **FIXED**: added it to the op list. [`tests/test_memory.py`]
- [x] [Review][Patch] Inaccurate dream-registration comment ("re-proposes next idle stretch"). **FIXED**: comment now states the deferred job's `last_run` advances → it waits for a fresh interaction + another idle stretch, not a same-stretch retry. [`shelldon/core/runtime.py:__init__`]
- [x] [Review][Patch] `gather_context` comment misstated the exception hierarchy. **FIXED**: reworded — "UnicodeError subclasses ValueError, not OSError — so it must be listed explicitly or it would escape this handler." [`shelldon/worker/prompt.py:gather_context`]
- [x] [Review][Defer] `facts/` surfacing follow-on — `remember`→`facts/` promotions are durable but not injected into later prompts; `rewrite_about`/`rewrite_summary` are the correct surfaced paths. Reinstate `remember` in the dream directive when 4.4 is extended to inject `facts/`. — deferred, noted in Dev Notes + CAP-11 test comment
- [x] [Review][Defer] Promoted learning can be permanently lost if process crashes between `resolve_learning` (applied first) and the corresponding `remember`/`rewrite_about` (applied after) — same crash-in-ops-loop risk as all `_apply_proposed_ops` paths; the dream is no worse than existing behavior. — deferred, general ops-loop risk
- [x] [Review][Defer→Fixed] No test for observations with embedded newlines baked into the dream directive — `\n` in observation text breaks the `- [id=N] … (seen N×)` line format. **FIXED NOW** (it matches the Epic 5 input-edge sub-list I added, and it's one line): `_build_dream_prompt` flattens via `' '.join(observation.split())`; +`test_dream_prompt_flattens_newlines_in_observations`. [`shelldon/core/runtime.py:_build_dream_prompt`]
- [x] [Review][Defer] Observation length unbounded — 50 multi-KB observations produce a very large dream prompt; `capture_learning` has no max-len cap. — deferred, pre-model
- [x] [Review][Defer] Blocking sqlite `pending_learnings()` read in async event loop during dispatch — safe on Pi Zero single-writer WAL; revisit when Epic 7 plugin-host adds concurrent disk pressure. — deferred, pre-Epic 7
- [x] [Review][Defer] Dream op vocab (`resolve_learning`, `rewrite_summary`) placed after the closing ``` fence in `SYSTEM_INSTRUCTION` — same deferred pattern as 6.1; fix when live-LLM prompt tuning introduced. [`shelldon/worker/prompt.py`] — deferred, consistent with 6.1

## Dev Notes

**This story is the dream cycle, built as a proactive-turn variant — not a new subsystem (AD-15).** The discipline: reuse the 5.4 `prompt_builder` + `history_owner_text` seams, the 5.2/5.3 gates, and the 4.5 propose→apply wire **verbatim**; add only the two ops, the two sqlite methods, the summary markdown path, and the dream prompt builder. Resist building a separate consolidation engine, a silent-turn lifecycle, or a vault path.

### The seams reused (read these first)

- [Source: `shelldon/core/runtime.py`] `_build_proactive_prompt` + the proactive `Job(..., prompt_builder=, history_owner_text=)` registration (5.4) — **copy this shape** for the dream (heavier `cost=3`, the dream marker, the 6h Idle). `_dispatch_turn_job`/`_resolve_job_prompt` (the empty-prompt → skip guard) are **unchanged** — an empty `_build_dream_prompt` (no pending learnings) skips for free. `_apply_proposed_ops` (the AddFace/CaptureLearning/else dispatch, guarded, capped, applied AFTER the reply) — add the `ResolveLearning` branch the same way; `RewriteSummary` falls through to `apply_memory_op`.
- [Source: `shelldon/core/history.py`] the `learnings` table (6.1) + `record_turn`'s one-commit WAL pattern. **Add `pending_learnings` (read) + `resolve_learning` (soft UPDATE)** mirroring it. The 6.1 dedup UPSERT + the `status` CHECK are already in place.
- [Source: `shelldon/core/memory.py`] `apply_memory_op`'s `isinstance` dispatch + `_apply_rewrite_about` + `_atomic_write_text` + `read_about` — **mirror for `RewriteSummary`/`read_summary`/`summary.md`**. `DIRECTIVE.md` stays structurally unreachable.
- [Source: `shelldon/worker/prompt.py`] `gather_context`/`assemble_prompt` (the AD-6-ordered, fail-soft reader that injects DIRECTIVE/about/recent/recall) — **add the `summary.md` read + section**; `SYSTEM_INSTRUCTION` (the op vocabulary) — add the dream ops. `build_prompt` is what the worker calls, so injecting summary here makes a promoted fact / summary reach later turns (CAP-11/AC3).
- [Source: `shelldon/contracts/__init__.py`] `MemoryOp` / `ProposedOp` / `CaptureLearning` (6.1) — `RewriteSummary` joins `MemoryOp` (markdown), `ResolveLearning` joins `ProposedOp` (sqlite). Additive variants, no version bump (AD-13), worker decodes them for free.
- [Source: `shelldon/worker/worker.py`] `run_worker`/`assemble`/`parse_reply` — **unchanged**. The dream prompt rides the owner-message slot (baked by core), the worker assembles + forwards it, parses the ops out of the reply. No worker change.

### Dream design (keep it minimal — AD-15 light scope)

- **The dream IS a turn.** Core builds the directive (reads pending learnings — core owns the store), the worker forwards it to the broker, the LLM replies with a brief note + an ops block, core applies the ops. Same lifecycle as a proactive turn; the only differences are the prompt content, the `cost`, and the history marker.
- **Promote = two ops, not one.** Writing the durable knowledge is an existing `remember`/`rewrite_about` (markdown); marking the *source* learning consumed is `resolve_learning(id, "promoted")` (sqlite). The LLM proposes both. Prune is just `resolve_learning(id, "pruned")`. This keeps the markdown writer and the learnings lifecycle cleanly separate (the 6.1 sqlite-vs-markdown split).
- **Soft transition, not delete.** `resolve_learning` only sets `status`; it never `DELETE`s. A pruned-but-recurring learning re-captures (6.1 UPSERT resets it to `pending`), so the dream sees it again with a higher `recurrence_count` — recurrence is the durability signal (AD-15 "impact + recurrence").
- **Reference by `id`, core-validated.** Core bakes `[id=N]` into the directive; the LLM echoes the id in `resolve_learning`. A hallucinated/stale id → the `WHERE id=? AND status='pending'` UPDATE is a 0-row no-op (logged, never raised). No trust placed in the LLM's id.
- **Summary is bounded context, not a transcript.** `rewrite_summary` overwrites `summary.md` with a short running summary; 4.4 injects it so later turns carry the gist without the full backlog. Light scope — no message-table compaction.
- **Empty dream is skipped.** No pending learnings → `_build_dream_prompt` returns "" → the 5.4 skip path fires (no spawn, no spend). The dream only costs budget when there's something to consolidate.

### Architecture invariants (binding)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-15** (142-145): "dreaming is a scheduled introspective WORKER TURN … reuses the fork-server, broker, and arbiter exactly like a normal turn" — (1) consolidate recent history, (2) classify `pending` learnings + promote durable ones to markdown (sensitive → vault, **deferred**), (3) prune the rest; the worker only **proposes**, core is sole writer. **LIGHT scope** — no taxonomy. 6.2 implements (1) as the summary, (2) minus vault, (3).
- **AD-14/AD-9** (137-140/112-115): the dream is a scheduler turn job through the arbiter — cooldown + daily budget + battery backoff. The scheduler never forks directly; 6.2's dream dispatches via `_dispatch_turn_job` (reused).
- **AD-5** (89-91): the worker proposes `resolve_learning`/`rewrite_summary`/`remember`; **core is the sole writer** of both sqlite and markdown.
- **AD-6** (93-97): learnings live in sqlite (status lifecycle); curated knowledge in markdown (`about.md`/`facts/`/`summary.md`). The promote step moves the durable signal from the firehose to the keepsakes.
- **AD-13**: additive op variants + `summary.md` — non-breaking; no `SCHEMA_VERSION` bump.
- **AD-1**: `contracts/`, `core/history.py`, `core/memory.py`, `core/runtime.py` stay LLM-free (import-linter KEPT).

### Testing standards

- `pytest`; deterministic — **synthetic dream `Result`s** (construct `Result(proposed_ops=[...])` directly, the 4.5 apply-test pattern), injected `now`, no sleep anchors, no live LLM. The LLM's classification judgment is explicitly NOT under test (note it); the apply mechanism + CAP-11 reflection ARE.
- Extend `tests/test_history.py` (pending read + resolve), `tests/test_memory.py` (summary), `tests/test_prompt_assembly.py` (summary injection), `tests/test_contracts_roundtrip.py` (the two variants), `tests/test_proposed_ops.py` (routing + CAP-11). A dream-dispatch test (pending → dispatch + cost 3; empty → skip) alongside the 5.4 dispatch suite.
- **Apply `dev-loop-checklist.md`** incl. the Epic 5 input-edge sub-list: `resolve_learning` validates the id/status at the DB (0-row no-op, never raises); empty summary content rejected; the markdown/sqlite end states asserted by real reads (not truthiness); the skip-when-no-pending branch tested; rejection paths (bad id, empty summary) tested; **no conftest change** (summary rides the memory root, learnings the history.db). Confirm no false-positive masking (the CAP-11 fact assertion isn't satisfied by the dream directive text).
- Run the **soak** (`-m soak`) — green + unchanged (dream job parked with the scheduler; no new db/file default path).

### Project Structure Notes

- New: none (additive ops + methods). Modified: `shelldon/contracts/__init__.py` (`ResolveLearning`→`ProposedOp`, `RewriteSummary`→`MemoryOp`, `__all__`), `shelldon/core/history.py` (`pending_learnings` + `resolve_learning`), `shelldon/core/memory.py` (`RewriteSummary` apply + `read_summary` + `summary.md`), `shelldon/worker/prompt.py` (inject `summary.md` + `SYSTEM_INSTRUCTION` dream ops), `shelldon/core/runtime.py` (dream defaults/marker, register the dream Idle job, `_build_dream_prompt`, route `ResolveLearning`), and the test suites.
- **Unchanged on purpose:** `shelldon/worker/worker.py` (the dream is a normal turn; the union decodes the variants for free), `shelldon/core/vault.py` + any vault write path (sensitive routing deferred), `shelldon/app.py` (jobs register in `Core.__init__`), the turn lifecycle (`_start_turn`/`_handle_result` — the dream rides them verbatim with the 5.4 marker).
- LLM-free core/contracts (AD-1) stays **KEPT**. No new real-`$HOME` write path (`summary.md` in the memory root, learnings in `history.db` — both already conftest-isolated).

### Previous-story intelligence (6.1 done; 5.4/5.2/4.5/4.4 are the substrate)

- **6.1 built the `learnings` table + `capture_learning`** (UPSERT dedup, status `pending`/`promoted`/`pruned` CHECK, soft transitions, reset-to-pending-on-recurrence). 6.2 is its consumer: read `pending`, transition to `promoted`/`pruned`. The CHECK + the UPSERT are done; **do not touch them**. 6.1's review chose the atomic UPSERT precisely so 6.2 (the second writer) is race-free.
- **5.4 built the `prompt_builder` + `history_owner_text` turn-job seam** and proved CAP-4 (a turn with no owner input). The dream is the same shape — copy `_build_proactive_prompt`/the proactive `Job` registration, swap mood→learnings, add `cost=3`. The 5.4 empty-prompt skip is exactly the "no pending learnings → no dream" behavior.
- **5.2 built the `cost` weight** anticipating "a future dream turn declares `cost=3`" — this is that turn. **5.3 battery** eases the dream off on battery (`essential=False`). Both gates are reused unchanged.
- **4.5 built the propose→apply wire**; **4.2 the markdown ops**; **4.4 the prompt assembly** that injects about/history into every turn (the CAP-11 delivery path). 6.2 adds two ops to the wire + one read to the assembly. **Epic 5 retro: keep `runtime.py` changes minimal** — the dream is one job registration + one builder + one dispatch branch; do not refactor the lifecycle.
- **Epic 5 retro input-edge sub-list:** `resolve_learning(id)` — a non-int / out-of-range / stale id must be a safe no-op (DB `WHERE`-guarded); `rewrite_summary` empty/whitespace content rejected; the dream directive must not crash if an observation contains odd characters (it's baked into a prompt string, not SQL — safe, but keep the formatting robust).

### Resolved decisions (owner, 2026-06-19 — binding)

1. **Trigger = `Idle` cadence (~6h owner silence), `cost=3`.** The dream is an Idle turn job like the proactive one (reuse 5.1 `Idle` + the 5.4 seam). Two Idle turn jobs now coexist (proactive 1h, dream 6h) — independent, fire-once-per-stretch; note the cold-start collision, don't coordinate.
2. **Vault routing for sensitive learnings = DEFERRED** (follow-on). No vault SURFACING exists yet (4.4), so a write-only vault path would be half a feature. 6.2 promotes to the readable markdown tree + prunes; sensitive→vault lands when surfacing is built. Trims the AD-15 vault clause — noted gap.
3. **History consolidation = INCLUDED, minimal.** A new `rewrite_summary(content)` op → `memory/summary.md`, injected by 4.4 into later turns. Light scope — no message-table compaction.
4. **Promote = `remember`/`rewrite_about` (markdown) + `resolve_learning(id, "promoted")` (sqlite); prune = `resolve_learning(id, "pruned")`; soft transition (no DELETE); reference by core-baked `id`.** (Implementation decisions, baked — flag if you disagree.)

## References

- [Source: `_bmad-output/planning-artifacts/epics.md`#Story-6.2 (lines 647-665)] — the three ACs verbatim.
- [Source: ARCHITECTURE-SPINE.md] AD-15 (142-145, the dream turn), AD-14/AD-9 (scheduler/arbiter gating), AD-5 (89-91, sole writer / worker proposes), AD-6 (93-97, sqlite learnings + markdown curated), AD-13, AD-1.
- [Source: `shelldon/core/runtime.py`] `_build_proactive_prompt` + proactive `Job` registration + `_dispatch_turn_job`/`_resolve_job_prompt` + `_apply_proposed_ops` — the seams reused.
- [Source: `shelldon/core/history.py`] the `learnings` table (6.1) + `record_turn` pattern — `pending_learnings`/`resolve_learning` join here.
- [Source: `shelldon/core/memory.py`] `apply_memory_op`/`_apply_rewrite_about`/`read_about` — `RewriteSummary`/`read_summary` mirror these.
- [Source: `shelldon/worker/prompt.py`] `gather_context`/`assemble_prompt`/`SYSTEM_INSTRUCTION` — the summary injection + dream op vocabulary.
- [Source: `shelldon/contracts/__init__.py`] `MemoryOp`/`ProposedOp`/`CaptureLearning` — the unions the new ops join.
- [Source: `_bmad-output/implementation-artifacts/6-1-capture-learnings-on-the-hot-path.md`] the learnings substrate + the UPSERT-for-race-safety decision.
- [Source: `_bmad-output/implementation-artifacts/5-4-proactive-action.md`] the `prompt_builder`/`history_owner_text` turn-job seam reused.
- [Source: `_bmad-output/implementation-artifacts/dev-loop-checklist.md`] the pre-review self-checklist (incl. the Epic 5 input-edge sub-list).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Amelia / bmad-dev-story)

### Debug Log References

- Full default suite: `uv run pytest -q` → **446 passed, 3 skipped** (platform fork/privdrop gates), 3 deselected (`live`).
- Soak: `uv run pytest -m soak -q` → **2 passed**, 1 skipped. Unchanged — the dream job is a resident turn job parked with the scheduler; no new db/file default path (`summary.md` rides the memory root, learnings the existing `history.db`).
- Contracts: `uv run lint-imports` → **2 kept, 0 broken** (`contracts/`/`core/*` stay LLM-free).
- New tests (+20): `tests/test_history.py` (+5 pending/resolve), `tests/test_memory.py` (+4 summary), `tests/test_prompt_assembly.py` (+3 summary injection), `tests/test_contracts_roundtrip.py` (+2 dream-ops roundtrip + bad-status reject), `tests/test_proposed_ops.py` (+4 routing/CAP-11/parse), `tests/test_turn_dispatch.py` (+3 dream dispatch/skip/cost).

### Completion Notes List

- **The dream is a proactive-turn variant** (AD-15, no new subsystem): a `Job("dream", Idle(6h), TURN, prompt_builder=_build_dream_prompt, history_owner_text="(shelldon dreamed)", cost=3)` registered in `Core.__init__` after the proactive job. Rides the 5.2/5.3 gates + the `_handle_result` lifecycle + the 5.4 marker — **zero new turn-lifecycle code** (Epic 5 retro coupling discipline). A brief owner-facing "tidied up" note goes out; the ops apply.
- **`_build_dream_prompt`** reads `self.history.pending_learnings()` (core read, impact-first), bakes each as `[id=N] observation (seen N×)` into a directive, and returns `""` when nothing is pending → the 5.4 empty-prompt skip fires (no dream, no spend). Proven by the dispatch tests (pending → spawn + spend 3; empty → skip).
- **Two new contract ops**, both additive (no `SCHEMA_VERSION` bump, decoded by the worker for free): `ResolveLearning(id, status: Literal["promoted","pruned"])` → `ProposedOp` (sqlite); `RewriteSummary(content)` → `MemoryOp` (markdown).
- **sqlite (`core/history.py`):** `pending_learnings(limit)` (read, `recurrence DESC`) + `resolve_learning(id, status)` (soft `UPDATE ... WHERE id=? AND status='pending'` — a stale/resolved id is a 0-row no-op, logged). Never a DELETE → a pruned-but-recurring learning resets to pending (6.1 UPSERT).
- **markdown (`core/memory.py`):** `RewriteSummary` → `_apply_rewrite_summary` (atomic `summary.md`, empty rejected) + `read_summary`. `summary.md` is bot-owned; `DIRECTIVE.md` still structurally unreachable.
- **prompt (`worker/prompt.py`):** `gather_context`/`assemble_prompt` inject `summary.md` as `# Conversation so far` (after `about`, before `recent` — AD-6 order), fail-soft; `SYSTEM_INSTRUCTION` gained the dream-op vocabulary.
- **routing (`runtime._apply_proposed_ops`):** one new `ResolveLearning` branch → `history.resolve_learning`; `RewriteSummary` falls through to `apply_memory_op`. Guarded/capped/after-the-reply (unchanged).
- **DISCOVERED GAP (recorded for review/follow-on):** the 4.4 prompt assembly injects `about.md` + `summary.md` + history recall, **but not `facts/`** — so a `remember`→`facts/` promotion is durable storage but NOT surfaced into later prompts. **CAP-11 (AC2) is therefore proven via `rewrite_about`/`summary` (the surfaced docs).** The dream still MAY promote to `facts/` (durable), but content that should *shape replies* must go to `about.md`/`summary.md`. Surfacing `facts/`/`people/` into prompts is a noted follow-on (a 4.4 extension, out of 6.2 scope).
- **Scope honored:** vault routing deferred (owner decision — no surfacing yet); history consolidation = the minimal `summary.md`; no message-table compaction; no context-pressure trigger; no live-LLM classification verification (synthetic dream `Result`s).
- **Two Idle turn jobs** (proactive 1h, dream 6h) noted in-code: independent, fire-once-per-stretch; a cold-start-while-already-idle collision defers one to next stretch (acceptable, no coordination added).
- **dev-loop-checklist applied (incl. input-edge sub-list):** `resolve_learning` id/status DB-guarded (no-op, never raises); empty summary rejected; real end-state assertions (markdown + sqlite reads); skip-when-no-pending + bad-id + bad-status branches tested; no conftest change (summary rides the memory root, learnings the history.db).

### File List

- `shelldon/contracts/__init__.py` (modified — `RewriteSummary`→`MemoryOp`, `ResolveLearning`→`ProposedOp`, `__all__`)
- `shelldon/core/history.py` (modified — `pending_learnings` + `resolve_learning`)
- `shelldon/core/memory.py` (modified — `RewriteSummary` apply + `read_summary` + `summary.md`)
- `shelldon/worker/prompt.py` (modified — inject `summary.md` + `SYSTEM_INSTRUCTION` dream ops)
- `shelldon/core/runtime.py` (modified — dream defaults/marker/cost, register the dream Idle job, `_build_dream_prompt`, route `ResolveLearning`)
- `tests/test_history.py`, `tests/test_memory.py`, `tests/test_prompt_assembly.py`, `tests/test_contracts_roundtrip.py`, `tests/test_proposed_ops.py`, `tests/test_turn_dispatch.py` (modified — new coverage)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (status tracking)

## Change Log

| Date | Change |
|------|--------|
| 2026-06-19 | Story 6.2 implemented: the dream cycle (AD-15/CAP-11) as a proactive-turn variant — `Idle(6h)` `cost=3` turn job (reuses the 5.4 `prompt_builder`/`history_owner_text` seam + 5.2/5.3 gates, no new lifecycle). `_build_dream_prompt` reads pending learnings + bakes them by id (empty → skip). New ops: `ResolveLearning`→`ProposedOp` (sqlite soft promote/prune), `RewriteSummary`→`MemoryOp` (→`summary.md`, injected by 4.4). `pending_learnings`/`resolve_learning` on the store; `summary.md` markdown path. CAP-11 proven via `rewrite_about`/summary (discovered: 4.4 doesn't inject `facts/` — surfacing it is a follow-on). Vault routing deferred (owner). +20 tests; suite 446 pass / soak 2 pass; contracts KEPT. |
| 2026-06-19 | Code-review follow-ups (6 Patches + 1 pulled-in Defer) resolved: dream directive steers to `rewrite_about` (the surfaced doc, not unsurfaced `facts/`); empty-builder skip → `log.debug` (no per-cadence prod spam); +tick-level dream cadence integration test; `RewriteSummary` added to the directive-safety test; fixed two inaccurate comments (dream cold-start defer behavior, `gather_context` exception hierarchy); newline-in-observation flattened in the baked directive (+test). 6 Defers accepted (facts/ surfacing follow-on, crash-between-ops general risk, obs length cap, blocking sqlite read pre-Epic-7, prompt-fence placement). +2 tests; suite 448 pass; contracts KEPT. |
