---
baseline_commit: d246e45
---

# Story 6.1: Capture learnings on the hot path

Status: done

<!-- First story of Epic 6 (Dreaming & Learning). Additive: a new sqlite-backed proposed-op. The dream cycle that consumes these learnings is Story 6.2. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to jot down things worth remembering as we talk,
so that nothing notable is lost before it can be consolidated.

**Why now / what it unblocks:** Epic 6 makes the pet improve over time. It has two halves: **cheap capture on the hot path (6.1)** and a **scheduled dream cycle that classifies + promotes + prunes (6.2)**. 6.1 is the capture half — a new `capture_learning(observation, pattern_key?)` proposed-op (AD-6) that a worker can emit on a *normal* turn's `Result`, which core writes to a new sqlite `learnings` table **with no extra LLM call**. It is the raw, queryable buffer the 6.2 dream turn later reads, judges, and consolidates. Until 6.1 exists, recurring self-observations vanish between turns; after it, they accumulate (deduped, recurrence-counted) waiting for the dream to keep what matters. This is a thin, fully-mechanically-testable story (like 3.4 on the 4.5 wire) — it adds one op to a wire that already exists.

**What's genuinely new (read first).** (1) A new `CaptureLearning` op in `contracts/` joins the `ProposedOp` union — but it is **NOT a curated-markdown op**: it routes to **sqlite**, not `apply_memory_op`. (2) A new `learnings` table in the **existing** `history.db` (AD-6 "plus a `learnings` table"), with `pattern_key` dedup + `recurrence_count` + `status`. (3) Core's proposed-op dispatch (`_apply_proposed_ops`) gains a third branch → the new history writer. The wire (worker parses `proposed_ops`, core applies them, single-writer AD-5) is **reused unchanged** from 4.5 — adding a tagged variant to the union makes the worker decode it for free.

## Acceptance Criteria

### AC1 — A proposed `capture_learning` writes a `pending` learnings row, no extra LLM call

**Given** the `capture_learning(observation, pattern_key?)` memory-op in `contracts/`
**When** a worker proposes it during a normal turn
**Then** core writes a row to a sqlite `learnings` table (created here) with `status='pending'` — **with no extra LLM call** (it rides the existing 4.5 proposed-ops apply path; capture is a plain sqlite insert on the turn that already happened, not a new fork/turn).

- `CaptureLearning` is a frozen, tagged, `forbid_unknown_fields` msgspec struct in `contracts/__init__.py` (the `Remember`/`AddFace` precedent): `observation: str`, `pattern_key: str | None = None`. It joins the **`ProposedOp`** union (so the worker may propose it and `worker.parse_reply`'s `list[ProposedOp]` decoder accepts it **with no worker change**) — but it is **NOT** added to `MemoryOp` (that union is the curated-markdown ops). Adding a union variant is a **non-breaking wire add** (AD-13) — no `SCHEMA_VERSION` bump.
- Core dispatches it (single writer, AD-5) to the **new sqlite writer**, NOT `apply_memory_op` / the markdown tree.

### AC2 — A recurring observation increments `recurrence_count` instead of duplicating

**Given** a recurring observation (same `pattern_key`)
**When** it is captured again
**Then** its `recurrence_count` increments (and the row refreshes to `status='pending'` with an updated `last_seen`) rather than creating a duplicate row.

- **Dedup is by `pattern_key` only.** A capture with a `pattern_key` that matches an existing row → `UPDATE` (increment `recurrence_count`, refresh `last_seen`, reset `status='pending'` so a previously promoted/pruned-but-recurring learning re-enters the dream's queue — AD-6 "refreshes the row at `status=pending`"). A capture whose `pattern_key` is `None` → **always a fresh `INSERT`** (no dedup key, so anonymous observations never collapse together). One commit per write (AD-6 batched).

### Out of scope (explicit — later stories)

- **The dream cycle** (classify pending learnings → promote durable ones to curated markdown / vault → prune the rest, consolidate history) — **Story 6.2**. 6.1 only *captures*; nothing reads or transitions `status` away from `pending` yet. The `promoted`/`pruned` status values exist in the schema CHECK from the start (6.2 sets them) but 6.1 never writes them.
- **A read path for pending learnings** (the read-only handle the 6.2 dream worker uses to classify) — **Story 6.2**. 6.1 creates the table + the write/dedup; the dream's reader method lands when the dream needs it.
- **Eliciting `capture_learning` from a real model at scale** — 6.1 adds a light mention to the worker `SYSTEM_INSTRUCTION` so the LLM *can* emit it, but its real-model effectiveness is unverifiable without a live LLM (no live-LLM lane); the **tested** contract is parse → route → write → dedup with synthetic ops.
- **A promotion/pruning taxonomy, learning categories, ERRORS/FEATURE_REQUESTS buckets, CLAUDE.md/skill extraction** — AD-15 "LIGHT scope"; never in Epic 6.
- **Table-growth bounding** beyond the existing per-turn `MAX_PROPOSED_OPS=16` cap — the 6.2 dream prunes; unbounded growth between dreams is acceptable for a single-owner pet (note the limitation, don't add a cap).

## Tasks / Subtasks

- [x] **Task 1 — The `CaptureLearning` contract (`contracts/__init__.py`) (AC1)**
  - [x] Add `CaptureLearning(msgspec.Struct, frozen=True, tag="capture_learning", forbid_unknown_fields=True)`: `observation: str`, `pattern_key: str | None = None`. Place it near the memory-ops with a docstring noting it is a **sqlite** op (not markdown) — it does NOT belong to `MemoryOp`.
  - [x] Extend `ProposedOp = MemoryOp | AddFace | CaptureLearning` (the closed set a worker may propose). Add `CaptureLearning` to `__all__`. Do **not** bump `SCHEMA_VERSION` (additive union variant = non-breaking, AD-13).
- [x] **Task 2 — The `learnings` table + writer (`core/history.py`) (AC1, AC2)**
  - [x] Add a `learnings` table to `_SCHEMA`: `id INTEGER PRIMARY KEY`, `pattern_key TEXT` (nullable — the dedup key), `observation TEXT NOT NULL`, `recurrence_count INTEGER NOT NULL DEFAULT 1`, `status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','promoted','pruned'))`, `first_seen TEXT NOT NULL`, `last_seen TEXT NOT NULL`. (`CREATE TABLE IF NOT EXISTS` — a pre-6.1 `history.db` gains the table on next `open()`; the messages tables are untouched.) Consider a `CREATE INDEX IF NOT EXISTS` on `pattern_key` for the dedup lookup.
  - [x] `HistoryStore.capture_learning(observation: str, pattern_key: str | None, now: datetime) -> None`: in ONE transaction (`with self._conn`) — if `pattern_key is not None` and a row with that `pattern_key` exists → `UPDATE` (`recurrence_count = recurrence_count + 1`, `last_seen = ts`, `status = 'pending'`); else `INSERT` (`recurrence_count=1`, `status='pending'`, `first_seen = last_seen = ts`). `pattern_key IS NULL` always inserts.
  - [x] **Reuse the WAL + single-writer + batched-commit pattern** of `record_turn`. Do NOT add a second connection or a new db file — the learnings table lives in the same `history.db` (AD-6). The worker's read-only handle still only exposes recall (no learnings read path until 6.2).
- [x] **Task 3 — Route the op in core's proposed-op dispatch (`core/runtime.py`) (AC1)**
  - [x] In `_apply_proposed_ops`, add a branch: `isinstance(op, CaptureLearning)` → `self.history.capture_learning(op.observation, op.pattern_key, datetime.now(UTC))`, BEFORE the `apply_memory_op` fallback (so a learning never reaches the markdown writer). Keep it inside the existing per-op `try/except` guard (a bad capture is logged + skipped, never crashes the turn — best-effort, like the memory-op path). Import `CaptureLearning` from `contracts`.
  - [x] No change to the cap, the ordering (ops applied AFTER the reply — 4.5/AC2), or the guard structure.
- [x] **Task 4 — Light worker prompt mention (`worker/prompt.py`) (AC1 enablement)**
  - [x] Add ONE line to `SYSTEM_INSTRUCTION`'s op vocabulary telling the pet it MAY emit a `capture_learning` op to privately jot a recurring observation worth remembering later (with an optional `pattern_key` to dedup recurrences) — keep it brief, in the existing ops-block format the example already shows. Note in a comment that the real-model effect is unverifiable without a live LLM (the mechanism is what 6.1 tests).
- [x] **Task 5 — Tests (AC1, AC2)**
  - [x] `contracts` round-trip: a `Result` carrying a `CaptureLearning` in `proposed_ops` encodes/decodes (msgpack) and `worker.parse_reply` decodes a `capture_learning` ops block into a `CaptureLearning` (the existing `list[ProposedOp]` decoder — proves the union add works with NO worker change). A typo'd tag / unknown field is a decode error (the whole-reject discipline).
  - [x] `tests/test_history.py` (extend): `capture_learning` inserts a `pending` row (assert the real column values — `recurrence_count == 1`, `status == 'pending'`, `observation`, `first_seen == last_seen`); a second capture with the **same `pattern_key`** increments to 2 and refreshes `last_seen` + resets `status` to `pending` (set it to `promoted` first, then re-capture, assert it flips back); a `pattern_key=None` capture **always inserts** a new row (two None captures of the same text → 2 rows); a different `pattern_key` is a distinct row.
  - [x] Routing/integration (extend `tests/test_runtime*`/the proposed-ops suite): a `Result` with a `CaptureLearning` op drives `_apply_proposed_ops` → a learnings row exists AND `apply_memory_op`/the markdown tree is untouched (spy or assert no markdown file written); a malformed capture (e.g. forced writer error) is logged+skipped, the turn/reply survives; capture happens with **no spawn** (no extra LLM — assert the spawner saw no new turn).
- [x] **Task 6 — Soak + full-suite + contracts**
  - [x] Soak (`-m soak`) green/unchanged — no new resident emitter; the learnings table rides the existing `history.db` (no new write-default path → **no conftest change**; call this out). Full `pytest` green; both import-linter contracts **KEPT** (`contracts/` + `core/history.py` stay LLM-free). Apply `dev-loop-checklist.md` (incl. the new input-edge sub-list).

### Review Findings

- [x] [Review][Decision→Fixed] Non-atomic dedup: SELECT+UPDATE is a TOCTOU race if a second writer (6.2 dream cycle) prunes rows concurrently. **DECISION: fix now (owner call — clean 6.2 hand-off, net-simpler code, atomic by construction).** Replaced SELECT+UPDATE+INSERT with a single `INSERT … ON CONFLICT(pattern_key) WHERE pattern_key IS NOT NULL DO UPDATE SET recurrence_count = recurrence_count + 1, last_seen = excluded.last_seen, status = 'pending'`, backed by a `UNIQUE` partial index `learnings_pattern_key_uq ON learnings(pattern_key) WHERE pattern_key IS NOT NULL` (NULL keys exempt → anonymous learnings always insert). +`test_dedup_is_db_enforced_atomic_upsert` (a raw duplicate INSERT of the same key is DB-rejected; two NULL inserts both succeed). Suite 426 pass; existing dedup tests unchanged (behavior-identical single-writer). [`shelldon/core/history.py:capture_learning` + `_SCHEMA`]
- [x] [Review][Defer] Unbounded `observation`/`pattern_key` string lengths — LLM could emit arbitrarily long strings; no length cap or truncation. Single-owner pet; 6.2 dream prunes the table; acceptable now. Add length guards at Epic 7 plugin-host boundary or when table-growth is measured. [`shelldon/core/history.py:capture_learning`, `shelldon/contracts/__init__.py:CaptureLearning`] — deferred, out of scope per spec
- [x] [Review][Defer] tz-naive `datetime` accepted silently by `capture_learning` signature — `now: datetime` with no tzinfo enforcement; same risk as `record_turn`. All current callers pass `datetime.now(UTC)`. Enforce at call sites when type annotations are systematically added. [`shelldon/core/history.py:capture_learning`] — deferred, consistent with existing API
- [x] [Review][Defer] `capture_learning` prompt example placed after the closing ` ``` ` fence — real-model uptake is unverifiable (no live LLM); mechanism (parse→route→write→dedup) fully tested. Revisit prompt copy when live-LLM testing is introduced. [`shelldon/worker/prompt.py`] — deferred, real-model uptake unverifiable per spec
- [x] [Review][Defer] `CREATE TABLE IF NOT EXISTS` doesn't migrate an existing `learnings` table with a different schema — pre-existing pattern across all tables; no migration framework exists. Add `ALTER TABLE ADD COLUMN` guards before any Pi deployment where a pre-6.1 db could exist. [`shelldon/core/history.py:_SCHEMA`] — deferred, pre-existing pattern
- [x] [Review][Defer] Integration tests access `core.history._conn` directly to verify learnings rows — no public read API for learnings until 6.2. Replace with `HistoryStore.list_learnings()` or equivalent when 6.2 adds the dream read path. [`tests/test_proposed_ops.py`] — deferred, 6.2 adds the read path

## Dev Notes

**This story adds one proposed-op to an existing wire — it does not build the dream.** The capture is cheap and synchronous: the worker already parses `proposed_ops` and core already applies them (4.5); 6.1 adds a tagged variant + a sqlite table + one dispatch branch. The trap to avoid is treating `capture_learning` like a markdown memory-op — it is **sqlite-only** and must never touch `CuratedMemory`/`apply_memory_op`.

### The wire being extended (read these first)

- [Source: `shelldon/contracts/__init__.py`] the memory-op structs (`Remember`/`RewriteAbout`/`LogEpisode` = `MemoryOp`), `AddFace`, and `ProposedOp = MemoryOp | AddFace`. **Add `CaptureLearning` and extend `ProposedOp`** (NOT `MemoryOp`). `Result.proposed_ops` is `list[ProposedOp]` with a default factory — the union add is non-breaking; **do not bump `SCHEMA_VERSION`** (the proposed_ops-default precedent).
- [Source: `shelldon/worker/worker.py`] `_OPS_DECODER = msgspec.json.Decoder(list[ProposedOp])` and `parse_reply`. Because the decoder is over the union, **adding `CaptureLearning` to `ProposedOp` makes the worker decode it automatically — no `worker.py` change.** The whole-reject discipline (a malformed ops block yields NO ops, reply uncorrupted) is preserved for free.
- [Source: `shelldon/core/history.py`] `_SCHEMA`, `HistoryStore.record_turn` (the WAL + single-writer + one-commit-per-write pattern to mirror), `open()`/`_ensure_schema` (`CREATE TABLE IF NOT EXISTS` is additive — a pre-6.1 db upgrades on open). **Add the `learnings` table + `capture_learning` writer here.** The read-only `HistoryReader` is unchanged (no learnings read path until 6.2).
- [Source: `shelldon/core/runtime.py`] `_apply_proposed_ops` (lines ~612-637) routes `isinstance(op, AddFace)` → `apply_add_face`, else → `apply_memory_op`, each guarded, capped at `MAX_PROPOSED_OPS`, applied AFTER the reply (4.5/AC2). **Add the `CaptureLearning` branch → `self.history.capture_learning(...)`** before the memory-op fallback. `self.history` is the `HistoryStore` (already constructed in `__init__`).
- [Source: `shelldon/worker/prompt.py`] `SYSTEM_INSTRUCTION` already documents the `remember` op + the ```ops fence format. Add a brief `capture_learning` mention (Task 4) so the model *can* emit it.

### Schema design (keep it minimal — AD-6, the raw capture buffer)

- The `learnings` table is the **raw, queryable** capture layer (AD-6) — the dream (6.2) is what judges/promotes. 6.1 writes only `pending`. Columns are exactly what 6.2 needs to classify: `observation` (the text), `pattern_key` (dedup + recurrence identity), `recurrence_count` (impact signal — "judged by impact + recurrence, not a rigid count"), `status` (pending/promoted/pruned lifecycle), `first_seen`/`last_seen` (recency). No category/taxonomy columns (AD-15 light scope).
- **`status` lifecycle:** 6.1 only ever writes/refreshes `pending`. The CHECK allows all three values from the start so 6.2 needs no migration. Resetting a recurring learning to `pending` on re-capture (AC2) means a learning the dream already pruned but that **keeps recurring** gets a second chance — desired (recurrence is the durability signal).
- **Single-owner now**, shaped for a non-breaking `chat_id`/`user_id` add later (mirror the `messages` table's AD-13 note) — do NOT add such a column now.

### Architecture invariants (binding)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-6** (93-97): memory is hybrid — sqlite (history **+ learnings**) and the markdown tree. The `learnings` table fields (`pattern_key` dedup, `recurrence_count`, `status` pending/promoted/pruned, `observation`, timestamps) and the "no extra LLM" hot-path capture are spelled out here verbatim. Core is the single writer; the insert-or-increment-and-refresh-to-pending behavior is AD-6's.
- **AD-5** (89-91): only core mutates the sqlite store; the worker **proposes** `capture_learning` on a `Result`, core validates+writes. The worker's handle is read-only.
- **AD-15** (142-145): dreaming (6.2) consumes these learnings; 6.1 is the capture half it names ("a worker may propose a `capture_learning` ... no extra LLM"). LIGHT scope — no taxonomy.
- **AD-13**: the union/schema add is non-breaking (additive variant, default-empty `proposed_ops`); no `SCHEMA_VERSION` bump.
- **AD-1**: `contracts/` and `core/history.py` stay LLM-free (import-linter KEPT).

### Testing standards

- `pytest`; deterministic (injected `now`, no sleep anchors). Extend `tests/test_history.py` for the table + dedup, the contracts round-trip suite for the union add + `parse_reply` decode, and the proposed-ops/runtime suite for the routing (learnings written, markdown untouched, no extra spawn, malformed-capture survives).
- **Apply `dev-loop-checklist.md`** incl. the new input-edge sub-list: assert real column values (not truthiness); test the dedup branch AND the `pattern_key=None` always-insert branch AND the status-reset-on-recurrence branch; the malformed/rejected capture path (guarded, turn survives); confirm **no conftest change** (the learnings table rides the already-isolated `DEFAULT_HISTORY_PATH` — no new write-default path); no false-positive masking (the learnings row assertion isn't satisfied by a leftover messages row).
- Run the **soak** (`-m soak`) — green + unchanged (no new emitter, no new db file).

### Project Structure Notes

- New: none (additive). Modified: `shelldon/contracts/__init__.py` (`CaptureLearning` + `ProposedOp` + `__all__`), `shelldon/core/history.py` (`learnings` table in `_SCHEMA` + `capture_learning` writer), `shelldon/core/runtime.py` (`_apply_proposed_ops` branch + import), `shelldon/worker/prompt.py` (one `SYSTEM_INSTRUCTION` line), and the test suites (`tests/test_history.py`, the contracts round-trip suite, the proposed-ops/runtime suite).
- **Unchanged on purpose:** `shelldon/worker/worker.py` (the union decoder picks up the new variant for free), `shelldon/core/memory.py` (capture_learning is sqlite, never markdown), `shelldon/app.py`. The 4.5 propose→apply wire is reused verbatim.
- LLM-free core/contracts (AD-1) stays **KEPT**. No new real-`$HOME` write path (the learnings table is in the existing `history.db`).

### Previous-story intelligence (Epic 5 done; 4.5/4.2/4.1 are the substrate)

- **4.5 built the propose→apply wire + the `ProposedOp` union dispatch** (`_apply_proposed_ops`, guarded, capped, applied after the reply). 6.1 adds one variant + one branch — **reuse the pattern exactly**; do not re-architect the dispatch (the Epic 5 retro flagged `runtime.py` coupling — keep this change minimal and isolated to the one branch).
- **4.1 built `history.py`** (WAL, FTS5, one-commit-per-turn, read-only worker handle). The learnings table is a sibling in the same store — mirror `record_turn`'s transaction shape; don't invent a new writer abstraction.
- **3.4 was the "genuinely thin" precedent** — an op riding an existing wire in a handful of edits. 6.1 is the same shape: contract variant + table + one dispatch branch + tests. Resist scope creep into 6.2's dream.
- **Epic 5 retro input-edge sub-list applies:** `observation` could be empty/whitespace (guard or accept? — an empty observation is useless; a `.strip()`-empty observation should be skipped/logged at apply, not written); `pattern_key` empty-string vs None (treat `""` distinctly from `None`? — recommend normalizing `""`→`None` so a blank key doesn't become a dedup bucket, OR document it; pick one and test it).

### Open decisions (sensible defaults baked — flag if you disagree)

1. **Dedup by `pattern_key` only; `None` always inserts** (AC2, AD-6). Baked.
2. **Re-capture resets `status` to `pending`** (AD-6 "refreshes the row at `status=pending`"). Baked.
3. **`SYSTEM_INSTRUCTION` gains a light `capture_learning` mention** so the model can emit it (the capture is hot-path-during-talk per the story). The mechanism is tested; the real-model elicitation is not (no live LLM). Baked — flag if you'd rather defer the prompt copy to 6.2.
4. **Empty/whitespace `observation` is skipped at apply** (logged, not written — a useless row); **blank `pattern_key` (`""`) normalizes to `None`** (no blank dedup bucket). Baked per the input-edge checklist — adjust if you want `""` preserved.

## References

- [Source: `_bmad-output/planning-artifacts/epics.md`#Story-6.1 (lines 631-645)] — the two ACs verbatim.
- [Source: ARCHITECTURE-SPINE.md] AD-6 (93-97, the learnings table fields + hot-path capture), AD-5 (89-91, single writer / worker proposes), AD-15 (142-145, the dream consumes these), AD-13 (non-breaking add), AD-1.
- [Source: `shelldon/contracts/__init__.py`] the memory-op/`ProposedOp` union 6.1 extends.
- [Source: `shelldon/core/history.py`] the store the `learnings` table + writer join.
- [Source: `shelldon/core/runtime.py`] `_apply_proposed_ops` — the dispatch branch added here.
- [Source: `shelldon/worker/worker.py`] `parse_reply` / `_OPS_DECODER` — decodes the new variant for free.
- [Source: `shelldon/worker/prompt.py`] `SYSTEM_INSTRUCTION` — the op vocabulary mention.
- [Source: `_bmad-output/implementation-artifacts/4-5-worker-proposes-ops-wire.md`] the propose→apply wire reused.
- [Source: `_bmad-output/implementation-artifacts/4-1-conversation-history-store.md`] the sqlite store pattern mirrored.
- [Source: `_bmad-output/implementation-artifacts/dev-loop-checklist.md`] the pre-review self-checklist (incl. the Epic 5 input-edge sub-list).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Amelia / bmad-dev-story)

### Debug Log References

- Full default suite: `uv run pytest -q` → **425 passed, 3 skipped** (platform fork/privdrop gates), 3 deselected (`live`).
- Soak: `uv run pytest -m soak -q` → **2 passed**, 1 skipped. Unchanged by 6.1 — no new resident emitter, no new db file (learnings rides the existing `history.db`).
- Contracts: `uv run lint-imports` → **2 kept, 0 broken** (`contracts/` + `core/history.py` stay LLM-free).
- New tests (+14): `tests/test_history.py` (+8 — insert/dedup/recurrence-reset/None-insert/blank-key-normalize/empty-skip/CHECK-constraint/reader-has-no-writer), `tests/test_proposed_ops.py` (+5 — parse decode, optional pattern_key, route-to-sqlite-not-markdown, failure-skip, mixed-batch), `tests/test_contracts_roundtrip.py` (+1 — Envelope msgpack roundtrip of the union variant).

### Completion Notes List

- **`CaptureLearning` contract** (`contracts/__init__.py`): frozen, tagged (`capture_learning`), `forbid_unknown_fields`; `observation: str` + `pattern_key: str | None = None`. Joins **`ProposedOp`** (NOT `MemoryOp` — it's sqlite, not markdown). No `SCHEMA_VERSION` bump (additive union variant — AD-13); proven by the Envelope msgpack roundtrip test.
- **`learnings` table + writer** (`core/history.py`): added to `_SCHEMA` (`pattern_key` nullable + indexed, `observation`, `recurrence_count`, `status CHECK IN (pending/promoted/pruned)`, `first_seen`/`last_seen`) — `CREATE TABLE IF NOT EXISTS`, so a pre-6.1 `history.db` upgrades on `open()`, messages tables untouched. `HistoryStore.capture_learning` mirrors `record_turn`'s one-commit WAL pattern: dedup by `pattern_key` (UPDATE: increment + refresh `last_seen` + reset `status='pending'`), else INSERT; `None`/blank key always inserts; empty/whitespace observation skipped (logged, never raised). Reader handle unchanged (no learnings read path until 6.2).
- **Routing** (`runtime._apply_proposed_ops`): added an `isinstance(op, CaptureLearning)` branch → `self.history.capture_learning(...)` BEFORE the `apply_memory_op` fallback, inside the existing per-op guard. **No worker.py change** — `worker.parse_reply`'s `list[ProposedOp]` decoder picks up the variant for free (proven by a parse test).
- **Prompt** (`worker/prompt.py`): one line added to `SYSTEM_INSTRUCTION` so the model *can* emit `capture_learning`. The mechanism (parse→route→write→dedup) is fully tested; real-model uptake is unverifiable without a live LLM (noted in-code).
- **No extra LLM call** — capture is a plain sqlite write on the turn that already happened; the routing test asserts no new spawn.
- **dev-loop-checklist applied (incl. the Epic 5 input-edge sub-list):** empty/whitespace `observation` skipped; blank `pattern_key` (`""`/whitespace) normalized to `None` (no blank dedup bucket); tests assert real column values + every branch (dedup, None-insert, status-reset, CHECK-constraint, failure-skip, markdown-untouched); **no conftest change** (learnings rides the already-isolated `DEFAULT_HISTORY_PATH`).
- **Scope held:** sqlite-only (never `CuratedMemory`); no read path / no dream / no status transitions away from `pending` (all 6.2); minimal change isolated to one dispatch branch (the Epic 5 retro's `runtime.py`-coupling flag).

### File List

- `shelldon/contracts/__init__.py` (modified — `CaptureLearning` struct + `ProposedOp` union + `__all__`)
- `shelldon/core/history.py` (modified — `learnings` table in `_SCHEMA` + `capture_learning` writer)
- `shelldon/core/runtime.py` (modified — `CaptureLearning` import + `_apply_proposed_ops` branch)
- `shelldon/worker/prompt.py` (modified — one `SYSTEM_INSTRUCTION` line)
- `tests/test_history.py` (modified — learnings table + dedup/normalize/skip)
- `tests/test_proposed_ops.py` (modified — parse + routing-to-sqlite + mixed batch)
- `tests/test_contracts_roundtrip.py` (modified — union-variant Envelope roundtrip)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (status tracking)

## Change Log

| Date | Change |
|------|--------|
| 2026-06-18 | Story 6.1 implemented: hot-path learning capture (AD-6/AD-15). New `CaptureLearning` op in `contracts/` joins `ProposedOp` (NOT `MemoryOp` — sqlite, not markdown). New `learnings` table in the existing `history.db` (pattern_key dedup, recurrence_count, status pending/promoted/pruned, first/last_seen). `HistoryStore.capture_learning` insert-or-increment + reset-to-pending-on-recurrence; `None`/blank key always inserts; empty observation skipped. Core routes it via `_apply_proposed_ops` → the history writer with NO extra LLM call; worker decodes the union variant with NO worker.py change. Light `SYSTEM_INSTRUCTION` mention. +14 tests; suite 425 pass / soak 2 pass; contracts KEPT. Dream cycle that consumes pending learnings = 6.2. |
| 2026-06-19 | Code-review follow-up (1 Decision → owner chose FIX NOW; 0 patches; 5 defers accepted; 8 dismissed): replaced the non-atomic SELECT+UPDATE dedup with a single atomic UPSERT (`INSERT … ON CONFLICT(pattern_key) WHERE pattern_key IS NOT NULL DO UPDATE …`) backed by a UNIQUE partial index — closes the TOCTOU before 6.2 becomes the second db writer; net-simpler code. +1 test (DB-enforced uniqueness; NULL keys exempt). Suite 426 pass / soak 2 pass; contracts KEPT. |
