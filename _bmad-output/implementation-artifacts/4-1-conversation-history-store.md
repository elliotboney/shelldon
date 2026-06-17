---
baseline_commit: 884586864069d4b5da35a573a8619d70cefb84e4
---
# Story 4.1: Conversation-history store

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want every message stored in order with keyword recall,
so that the pet can remember and reference what we've said (CAP-6, AD-6, AD-5).

## Acceptance Criteria

1. **Core writes each completed turn to an ordered, timestamped sqlite store with FTS5 recall:** Given core, when a turn completes, then core writes **both the owner message and the pet reply** to a sqlite store (`~/.shelldon/history.db`) in **WAL mode** with **batched commits** (one transaction per turn, not per row), **ordered and timestamped** (ISO-8601 UTC), and an **FTS5 index** supports keyword recall over message content. Core is the **sole writer** (AD-5/AD-6). The path is **injectable** (tests never touch real `$HOME`).
2. **Workers read the store read-only and cannot write it:** Given a worker that needs conversation context, when it opens the store, then it gets a **read-only** handle (`mode=ro`) — recall/recent queries work, but any write **raises** rather than mutating. (3.4-style note: true *uid-level* isolation is Story 4.3's vault concern; 4.1 enforces read-only at the **connection** level and ships the reader seam. The worker's *use* of it during prompt assembly is **Story 4.4**.)
3. **Single-owner schema, shaped for a non-breaking multi-user add:** Given the schema, when designed, then it is single-owner now but **shaped so a `chat_id`/`user_id` key is a later non-breaking add** (a nullable `ALTER TABLE ADD COLUMN`, never a destructive migration) — architected, **not implemented** (AD-13).

> **Scope seam (binding):** 4.1 builds the **history substrate only** — the sqlite schema (`messages` + FTS5), a WAL/batched-commit writer owned by core that records each completed turn, a read-only reader/recall API, and read-only write-denial. It does **NOT** build: the **memory-ops contract / markdown tree** (`remember`/`rewrite_about`, the worker-proposes→core-applies protocol) — that is **Story 4.2** (and it carries the retro-flagged turn-topology decision); the **vault / uid isolation** — **Story 4.3**; **prompt assembly / injecting history+recall into the turn** — **Story 4.4** (4.1 stores and exposes recall; nothing reads it into a prompt yet); the `learnings` table (AD-6) and the **dream cycle** (Epic 6). The single biggest mistake here is building 4.2's memory-ops or 4.4's prompt injection inside 4.1.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent):**
> - **Core already has both halves of a turn.** `Core._start_turn(prompt)` holds the owner text (the folded prompt); `Core._handle_result(env)` holds the reply (`result.payload`). So core can record `(owner, reply)` on turn completion with **no worker/topology change** — stash the prompt per in-flight turn (≤1, AD-9) and consume it when the Result lands. [Source: shelldon/core/runtime.py:_start_turn, _handle_result, _degrade]
> - **`sqlite3` is stdlib (Python 3.13)** — no pin, no new dependency. FTS5 ships with the CPython-bundled sqlite on the Pi OS target (AD-6). Verify at init / in a test; fail loud if absent. [Source: ARCHITECTURE-SPINE.md#AD-6, #tech-stack (sqlite3 stdlib)]
> - **The Story 3.1 atomic-write + corruption-tolerance discipline applies to the *file path/dir*, not the rows** — sqlite owns durability via WAL. Reuse 3.1's "inject the path, create the parent dir, default `~/.shelldon/…`, never touch real `$HOME` in tests" pattern. [Source: shelldon/core/state.py, shelldon/core/faces.py]
> - **Recurring lesson (Epic 3 retro action #3):** any new core file-write must be isolated from real `$HOME` in the autouse conftest fixture **in this same change** — extend `tests/conftest.py` to redirect the history default path, don't discover it in verify. [Source: tests/conftest.py:_isolate_state_checkpoint, epic-3-retro-2026-06-17.md]
> - **Core is LLM-free (AD-1)** and stays so — `sqlite3` is stdlib, not a provider lib; the import-linter forbidden list is unaffected. History lives in `core/` (core owns memory, AD-5). [Source: pyproject.toml#tool.importlinter]

- [x] **Task 1: The sqlite history store — schema, WAL, FTS5** (AC: 1, 3)
  - [x] Create `shelldon/core/history.py`. On connect: open `~/.shelldon/history.db` (injectable path; create parent dir), set `PRAGMA journal_mode=WAL`, and create (if absent) a `messages` table — `id INTEGER PRIMARY KEY, turn_id TEXT, role TEXT NOT NULL CHECK(role IN ('owner','pet')), content TEXT NOT NULL, ts TEXT NOT NULL` (ISO-8601 UTC). `id` gives stable insertion order; `ts` is the human timestamp.
  - [x] Create an **FTS5** index over `content` (external-content FTS5 mirroring `messages`, kept in sync via insert triggers — the standard pattern — OR a manual FTS insert inside the same transaction as the row). Verify FTS5 is available at init; raise a clear error if the build lacks it (don't silently degrade recall).
  - [x] **Schema extensibility (AC3):** no constraint or design that would force a destructive migration to add `chat_id`/`user_id` later — those land as **nullable columns** via `ALTER TABLE ADD COLUMN` in a future story. Add a comment documenting this so a later dev doesn't "helpfully" make them NOT NULL. Do **not** add the columns now.

- [x] **Task 2: Writer API — record a turn in one batched transaction** (AC: 1)
  - [x] `HistoryStore.record_turn(turn_id, owner_text, pet_text, now)` inserts the owner row then the pet row (ordered) **in a single transaction = one commit per turn** (the "batched commits" of AD-6 — bounds write frequency vs a commit-per-row; WAL keeps the fsync cheap). Keep the FTS index in sync within the same transaction.
  - [x] Core is the **sole writer**: the writer connection lives in `Core` for the process lifetime (like the 3.1 checkpoint). Open on startup, close on teardown. Do not open a writer anywhere else.

- [x] **Task 3: Read-only reader + recall API** (AC: 1, 2)
  - [x] Provide read access: `recent(n)` (the last N messages in order) and `search(query, n)` (FTS5 keyword recall, most-relevant/most-recent). These are what Story 4.4's prompt assembly will call.
  - [x] Provide a **read-only opener** — `open_readonly(path)` (or `HistoryReader`) using a `file:…?mode=ro` URI connection (`sqlite3.connect(uri=True)`), exposing `recent`/`search` but **no write path**. A write attempted on the ro connection must raise `sqlite3.OperationalError`. This is the seam a worker uses; the worker's actual use is 4.4.

- [x] **Task 4: Wire core to record each completed turn** (AC: 1)
  - [x] In `Core`, construct the `HistoryStore` on startup (injectable `history_path`, default `~/.shelldon/history.db`). Stash the in-flight prompt in `_start_turn`; on a completed turn, call `record_turn(turn_id, owner_prompt, reply)`:
    - success Result → `(prompt, result.payload)`
    - degrade (failure Result OR turn timeout) → `(prompt, DEGRADE_TEXT)` (the pet's actual reply that turn)
    - spawn-failure path (turn never ran) → record nothing
  - [x] Keep it surgical — do not change turn ordering, fencing, or the arbiter. Recording is a side effect after the reply is emitted. Close the store cleanly in `_cleanup()`.

- [x] **Task 5: Tests** (AC: 1, 2, 3)
  - [x] **AC1:** record a turn → both rows present, correct `role`/order/`ts`; WAL mode is on (`PRAGMA journal_mode` == 'wal'); FTS5 `search()` returns a turn by a keyword in its content; assert one commit per turn (e.g. two rows appear atomically). Injected `tmp_path` db — never real `$HOME`.
  - [x] **AC2:** `open_readonly` `recent`/`search` work; an `INSERT`/`record` through the read-only connection **raises** `sqlite3.OperationalError`; the writer connection still works.
  - [x] **AC3:** `ALTER TABLE messages ADD COLUMN user_id TEXT` succeeds against the shipped schema and existing rows read back with `user_id IS NULL` — proving the non-breaking-add shape (the test documents the contract; the column is not kept).
  - [x] **Core integration:** drive a turn through `Core` (reuse the `tests/test_end_to_end_turn.py` harness or a focused unit) and assert the `(owner, pet)` pair landed; drive a **degrade** turn and assert `(prompt, DEGRADE_TEXT)` landed. Prefer state-predicate polling over sleeps (Epic 2 retro #1).
  - [x] Extend the autouse `tests/conftest.py` isolation fixture to redirect the history default path to `tmp_path` (Epic 3 retro action #3 — proactive, same change).

- [x] **Task 6: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (`sqlite3` is stdlib; no provider import enters `core/`; AD-1 holds).
  - [x] `uv run pytest -q` → green (existing 196 unchanged + the new history tests). Default run hits no network and writes no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **AD-6 — Hybrid memory; conversation history → sqlite:** "Conversation history → sqlite (one file, `~/.shelldon/history.db`). An ordered, timestamped **messages** store with **FTS5** keyword recall … sqlite runs in **WAL** mode with **batched commits** to bound write frequency." 4.1 builds exactly the history half (the `learnings` table + dream cycle are later). [Source: ARCHITECTURE-SPINE.md#AD-6]
- **AD-5 — Core is the sole writer:** "only `core` mutates … the sqlite conversation store. Workers **never** write … workers read the conversation store **read-only** (read-only sqlite handle)." 4.1's writer is in-core; the reader is `mode=ro`. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-13 — non-breaking multi-user shape:** "the conversation schema is shaped so `chat_id`/`user_id` is a **non-breaking add**" — nullable column later, not now. [Source: ARCHITECTURE-SPINE.md#AD-13]
- **AD-1 — LLM-free core:** history is `core/` + stdlib `sqlite3`; import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **AD-7 / NFR7 — SD-wear discipline:** WAL + per-turn batched commits bound write frequency (history is not high-churn like reflex state, but the same write-wear discipline applies). [Source: ARCHITECTURE-SPINE.md#AD-7, epics.md#NFR7]
- **Consistency Conventions — sqlite WAL + batched commits; one writer:** restated in the spine's cross-cutting table. [Source: ARCHITECTURE-SPINE.md#State & cross-cutting]

### Design guidance (what to build, minimally)

- **One file, `core/history.py`.** Mirror the flat `state.py`/`reflexes.py`/`faces.py` layout. When Story 4.2 adds the markdown tree, memory may graduate to a `core/memory/` package — leave that to 4.2; a single module is the minimal form now. [Source: ARCHITECTURE-SPINE.md#Structural-Seed (`core/ … memory/(owner)`)]
- **Writer lives in core, opened once.** Like the 3.1 checkpoint, `Core` owns the single write connection for the process lifetime (WAL allows concurrent readers). Readers open their own `mode=ro` connections.
- **Batched = per-turn transaction.** Insert both rows + FTS sync in one `with conn:` transaction (one commit). This is the AD-6 "batched commits" unit — durable to the last completed turn, not per-row chatty. A larger N-turn batch is a later tuning if write-wear ever demands it; don't pre-build it.
- **FTS5 external-content pattern.** Prefer external-content FTS5 (`content='messages', content_rowid='id'`) + insert trigger to avoid duplicating text; or a plain manual FTS insert in the same transaction. Either is fine — keep it append-only-simple (history is never edited/deleted in 4.1).
- **Read-only is connection-level here.** `mode=ro` denies writes at the sqlite layer — sufficient for 4.1's "worker cannot write." The stronger uid-level isolation (so a prompt-injected worker physically can't reach `vault/`) is Story 4.3 and is about the markdown vault, not history.
- **Inject the path; never write real `$HOME`.** Default `~/.shelldon/history.db`, but `Core(history_path=…)` and the store accept an injected path. Extend the conftest autouse fixture in the same change (the Epic 3 retro lesson — caught twice before).

### What 4.1 does NOT do

- **No memory-ops, no markdown tree** (`remember`/`rewrite_about`/`about.md`/`facts/`/`people/`) — Story 4.2. 4.1 introduces NO `contracts/` change and NO worker-proposes/core-applies protocol.
- **No worker-topology change** — core records history from what it already holds. The "worker proposes" reshape is a 4.2 decision (retro-flagged); keep it out of 4.1.
- **No prompt assembly / history injection** — Story 4.4. 4.1 stores + exposes recall; nothing reads it into a turn yet.
- **No vault / uid isolation** — Story 4.3.
- **No `learnings` table, no dream cycle** — AD-6 learnings + Epic 6.
- **No `chat_id`/`user_id` columns** — shaped-for only (AC3).
- **No history editing/deletion/retention policy** — append-only for now; pruning/summarization is the dream cycle (Epic 6) / 4.4 working-window concerns.

### Project Structure Notes

- **New:** `shelldon/core/history.py` (schema + WAL connect + FTS5 + `HistoryStore` writer + read-only reader; `DEFAULT_HISTORY_PATH`). New tests `tests/test_history.py`.
- **Modified:** `shelldon/core/runtime.py` — construct `HistoryStore` on startup (injectable `history_path`); stash the in-flight prompt in `_start_turn`; record `(owner, reply)` on each completed/degraded turn; close in `_cleanup()`. `tests/conftest.py` — autouse fixture also redirects `DEFAULT_HISTORY_PATH` to `tmp_path`.
- `core/` only (+ stdlib `sqlite3`) → import-linter KEPT.

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Use a `tmp_path` db for every test — **never real `$HOME`** (extend the autouse fixture). Assert on rows/order/timestamps, WAL pragma, FTS recall hits, read-only write-denial, and the `ALTER TABLE` non-breaking shape. For the core-integration test, reuse the 1.8 harness and assert the pair landed via a state predicate (no sleep anchors — Epic 2 retro #1). Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: epic-2-retro-2026-06-17.md, epic-3-retro-2026-06-17.md]

### Previous story intelligence (Epic 3 — just completed)

- **Reuse the durability/path idiom from 3.1/3.3:** injectable default path under `~/.shelldon/`, create the parent dir, and isolate it in the conftest fixture. The "never write real `$HOME`" bug was caught in verify **twice** in Epic 3 — bake the conftest redirect in from the start (retro action #3). [Source: shelldon/core/state.py, shelldon/core/faces.py, tests/conftest.py]
- **Recurring review themes to pre-empt:** guard inputs; never silently swallow (log on fallback, raise on a missing FTS5 build rather than degrading); no WHAT-comments (explain WHY); share test helpers via `conftest.py` (`await_true`, `DummySpawner`); value-not-truthiness asserts. Building these in avoids a repeat review cycle. [Source: 3-1/3-2/3-3 Review Findings, epic-3-retro-2026-06-17.md]
- **Background-emitter caution:** 4.1 adds no new background task, but if a later tweak does, park its interval in the turn-counting harnesses (the 1.9 soak's `_seq`/`_bg` invariants). [Source: epic-3-retro-2026-06-17.md]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 4 / Story 4.1 (this story); #Story 4.2 (memory-ops — next, carries the turn-topology decision); #Story 4.4 (prompt assembly — consumes recall); #FR6]
- [Source: ARCHITECTURE-SPINE.md#AD-6 (sqlite history, WAL, FTS5, batched commits), #AD-5 (core sole writer, read-only worker), #AD-13 (non-breaking multi-user shape), #AD-1 (LLM-free core), #AD-7 (SD-wear), #Structural-Seed (`core/ … memory/`)]
- [Source: shelldon/core/runtime.py (`_start_turn`/`_handle_result`/`_degrade`/`_cleanup` — where the turn pair is recorded; the in-flight-prompt stash point)]
- [Source: shelldon/core/state.py, shelldon/core/faces.py (injectable-path + parent-dir-create + corruption/idiom to mirror)]
- [Source: tests/conftest.py (`_isolate_state_checkpoint` autouse fixture to extend for the history path; `await_true`/`DummySpawner`)]
- [Source: _bmad-output/implementation-artifacts/epic-3-retro-2026-06-17.md (action #3 proactive `$HOME` isolation; recurring review themes)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- `uv run pytest -q` → 208 passed, 2 skipped, 3 deselected (196 prior + 12 new history tests).
- `uv run lint-imports` → both contracts KEPT (`sqlite3` is stdlib; no provider import enters `core/`).
- FTS5 confirmed available (sqlite 3.47.1). `~/.shelldon` never created by a run (autouse fixture redirects `DEFAULT_HISTORY_PATH`).
- Note: `uv run` rebuilds and hit a transient pypi timeout; used `uv run --no-sync` to run against the already-synced env.

### Completion Notes List

- **AC1** — `core/history.py` opens `~/.shelldon/history.db` in WAL, creates a `messages` table + external-content FTS5 index (insert-trigger synced), and `record_turn` writes the owner then pet row in **one transaction (one commit/turn)**, ISO-8601 UTC timestamped, `id`-ordered. `search()` does FTS5 keyword recall; `recent()` returns the last N chronologically. Init raises a clear `RuntimeError` if the sqlite build lacks FTS5 (no silent degrade).
- **AC2** — `open_readonly(path)` returns a `HistoryReader` over a `file:…?mode=ro` connection: `recent`/`search` work, it exposes no writer method, and a raw write through it raises `sqlite3.OperationalError`. (uid-level isolation remains Story 4.3's vault concern.)
- **AC3** — schema is single-owner with no constraint blocking a later nullable `ALTER TABLE messages ADD COLUMN user_id` (tested: existing rows read back NULL); a comment warns future devs not to make such a column NOT NULL.
- **Core wiring** — `Core` opens the store on startup (injectable `history_path`), stashes the in-flight prompt/turn_id in `_start_turn`, and records via `_record_turn` on the success path (`result.payload`) and the degrade path (`DEGRADE_TEXT`, covering failure-Result AND timeout); a spawn that never produced a reply records nothing (tested). Store closed in `_cleanup`. No change to turn ordering/fencing/arbiter.
- **Retro action #3 applied proactively** — extended the autouse conftest fixture to redirect `DEFAULT_HISTORY_PATH` to `tmp_path` in the same change; verified no real `$HOME` write.
- **Scope held** — no memory-ops/markdown (4.2), no vault (4.3), no prompt injection (4.4), no `learnings`/dream cycle, no worker-topology change, no `chat_id`/`user_id` columns, no `contracts/` change.

### File List

- `shelldon/core/history.py` (new) — `HistoryStore` writer (WAL + FTS5 + per-turn batched `record_turn` + `recent`/`search`), `HistoryReader` + `open_readonly` (read-only seam), `DEFAULT_HISTORY_PATH`, FTS5-availability guard.
- `shelldon/core/runtime.py` (modified) — construct `HistoryStore` on startup (injectable `history_path`); stash prompt/turn_id in `_start_turn`; `_record_turn` on success + degrade; close in `_cleanup`.
- `tests/test_history.py` (new) — 12 tests across AC1/AC2/AC3 + core integration (completed + degraded turn) + spawn-failure-records-nothing + FTS5-missing guard.
- `tests/conftest.py` (modified) — autouse fixture also redirects `DEFAULT_HISTORY_PATH` to `tmp_path`.

## Change Log

| Date       | Change                                                                 |
|------------|------------------------------------------------------------------------|
| 2026-06-17 | Implemented Story 4.1: conversation-history store — core-owned sqlite (WAL + FTS5), per-turn batched `record_turn`, read-only worker reader, non-breaking multi-user schema shape; core records each completed/degraded turn. All ACs met; 208 tests green; contracts KEPT; no real `$HOME` writes. |
| 2026-06-17 | Addressed code review (1 Medium): history persistence is now best-effort — `_record_turn` + `history.close()` log-and-continue on failure rather than crashing the turn loop. +1 test; 209 green; contracts KEPT. |

## Review Findings

- [x] **Medium** — history persistence called inline from success/degrade paths (+ `history.close()` in cleanup) with no exception handling; a sqlite failure after the reply is emitted would raise and take down the turn loop. — RESOLVED: `_record_turn` wraps the write in `try/except Exception` → `log.warning` + continue (best-effort bookkeeping, matching the 3.1 checkpoint-flush philosophy); `history.close()` in `_cleanup` is likewise guarded. Tested: a raising `record_turn` no longer propagates (`test_history_write_failure_does_not_crash_the_turn`).
