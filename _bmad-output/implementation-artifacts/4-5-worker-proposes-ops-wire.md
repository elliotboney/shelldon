---
baseline_commit: b036ec4ba6ce2709ca34bc619f5eb4c40c4ee27e
---

# Story 4.5: Worker proposes ops over the Result (the write-back wire)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to actually act on what it decides to remember or change,
so that the apply-halves built in 4.2 (`apply_memory_op`) and 3.3 (`apply_add_face`) are reachable from a real turn (AD-5, AD-2, AD-3, AD-12).

## Acceptance Criteria

1. **Topology reshape — worker emits the Result; broker returns the completion to the worker (broker stays pure egress):** Given a turn completes, when the broker finishes the provider-chain call, then the broker returns the **completion** to the **worker** (a new `COMPLETION` envelope routed `COMPLETION→WORKER`) — NOT a `Result→core` — and the **worker** parses its own reply into a structured `Result` (`payload` + a closed `proposed_ops` list) and sends `Result→core` (now `src=WORKER`). This reshapes the fire-and-forget worker + the `RESULT→CORE` emission from Stories 1.5/1.8 while **preserving `turn_id` fencing (AD-12) and the ≤1-worker bound (AD-9)**. The broker does **no pet-domain parsing** — it still only calls the provider and relays text/error (AD-2).
2. **Core applies the proposed ops via the existing single-writer apply paths; bad proposals are rejected without side effects and never affect the reply:** Given a `Result` carrying `proposed_ops`, when core fences and accepts it, then **after** delivering the user-facing reply + face, core validates and applies each op via the existing apply paths — `apply_memory_op` (Story 4.2) for memory-ops (`apply_add_face` for faces is Story 3.4, a later thin add) — as the **sole writer** (AD-5). An **invalid op is rejected** (logged, skipped) and an **oversized proposal** (too many ops) is **rejected/capped** — both **without side effects on the reply or on the rest of the turn**; one bad op never crashes the turn loop.
3. **`proposed_ops` is a non-breaking, closed addition to `Result` (AD-13/AD-6):** Given the `Result` contract, when `proposed_ops` is added, then it is an **optional field defaulting to empty** (`list[MemoryOp]`, the closed union from 4.2) — old plain replies (no ops) decode and behave exactly as today; the schema version is **not** bumped (additive field, AD-13). A contract round-trip test covers a `Result` with and without `proposed_ops`, and the new `COMPLETION` kind/body.

> **Scope seam (binding):** 4.5 builds the **shared write-back wire** for proposed ops: the `COMPLETION` broker→worker hop, the worker's reply→`proposed_ops` parse, `Result.proposed_ops`, and core's fenced apply loop calling `apply_memory_op`. It does **NOT** build: **real prompt assembly / memory injection** — Story 4.4 (the worker still receives the prompt verbatim from core; it does not yet read history/`about.md`/`DIRECTIVE.md`); the **LLM actually deciding ops from a designed prompt** — 4.4 writes the prompt that elicits the ops block; 4.5 builds + tests the *parse* against canned completions; the **faces op in `proposed_ops`** — Story 3.4 widens the union with an `AddFace` op and adds the `apply_add_face` dispatch branch (4.5 leaves the dispatch open for it); **`capture_learning` / the learnings table / dream cycle** — Epic 6. The single biggest mistake here is letting the broker parse ops (AD-2 violation) or building 4.4's prompt-side ops protocol inside 4.5.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent) — verified against the code:**
> - **The broker emits the Result→core TODAY; that is exactly what moves.** `broker/service.py:_serve_connection` reads a `Job`, calls `handle_job_chain`, then builds `Envelope(kind=RESULT, src=BROKER, dst=CORE, body=result, turn_id=env.turn_id)` and `write_frame`s it (the hub re-routes `RESULT→CORE`). 4.5 changes this to emit a `COMPLETION→WORKER` instead. [Source: shelldon/broker/service.py:54-64]
> - **The worker fire-and-forgets TODAY.** `worker/worker.py:run_worker` connects as `WORKER`, writes one `Job`, and closes — it does NOT read a reply. 4.5 makes it stay connected, read the `COMPLETION`, parse → `Result`, and emit `RESULT→CORE`. [Source: shelldon/worker/worker.py:17-37]
> - **The hub routes purely by `kind`** (`ROUTING_TABLE[env.kind]`), and a connection is registered by the `src` of its registration frame; a frame routed to `WORKER` is written to the registered `WORKER` connection. The ≤1-worker bound (AD-9) guarantees exactly one `WORKER` registration at a time, so `COMPLETION→WORKER` is unambiguous. [Source: shelldon/core/bus/server.py:128-144, :91-93]
> - **`Envelope` enforces kind↔body agreement** in `__post_init__` via `_KIND_FOR_BODY`, so a new hop needs BOTH a new `MsgKind` AND a new body struct registered in that map and the `body` union — you cannot send a `Result` body under a `COMPLETION` kind. [Source: shelldon/contracts/__init__.py:119-147, :110-116]
> - **`Result` already surfaces failure as a value, not an exception** (`ok`/`payload`/`error`). The broker's `handle_job_chain` returns a `Result` (ok or failure). Keep that — the worker turns a failure completion into `Result(ok=False, error=...)` so core's existing `_degrade()` path is unchanged. [Source: shelldon/contracts/__init__.py:58-65, shelldon/broker/broker.py:61-75]
> - **Core already fences + replies + records in `_handle_result`**: `fence.accept` → `_disarm_timeout` → `fence.close` → on `ok`: `_send_reply` + `_push_face(FACE_REPLY)` + `_record_turn`; else `_degrade`; then coalesce. 4.5 inserts op-application AFTER the reply/face, BEFORE/around `_record_turn`, guarded best-effort. The `src` of the Result is not inspected, so changing it BROKER→WORKER needs no fence change. [Source: shelldon/core/runtime.py:175-191, shelldon/core/turn.py:43-46]
> - **`Core.apply_add_face` already exists** (the 3.3 apply path passthrough). The memory equivalent does NOT — `apply_memory_op` lives on `CuratedMemory` (`core/memory.py`), which Core does not yet hold. 4.5 adds `self.memory = CuratedMemory(...)` + a thin `Core.apply_memory_op` passthrough (the "wire story's call" 4.2 deferred). [Source: shelldon/core/runtime.py:333-337, shelldon/core/memory.py:CuratedMemory]
> - **`MemoryOp` union is defined and closed** (`Remember | RewriteAbout | LogEpisode`, tagged, `forbid_unknown_fields`) — attach it to `Result.proposed_ops` now; do NOT redefine it. [Source: shelldon/contracts/__init__.py (Memory-ops section), shelldon/core/memory.py]
> - **Atomic-write isolation discipline:** Core constructs a `CuratedMemory` whose default root is `DEFAULT_MEMORY_ROOT` (~/.shelldon/memory). The conftest autouse fixture must redirect the name **runtime imports** off real `$HOME` in THIS change (Epic 3 retro #3 — the existing fixture already redirects `_runtime.DEFAULT_FACES_PATH`/`DEFAULT_HISTORY_PATH` this way). [Source: tests/conftest.py:_isolate_state_checkpoint]

- [x] **Task 1: Contracts — `COMPLETION` hop + `Result.proposed_ops`** (AC: 1, 3)
  - [x] Add `MsgKind.COMPLETION = "completion"` and a frozen `Completion(ok: bool, payload: str = "", error: str | None = None)` struct (tagged `"completion"`, `forbid_unknown_fields`) — the broker→worker carrier (text/error only; NO ops — AD-2). Register it in the `Envelope.body` union and `_KIND_FOR_BODY`, and add `ROUTING_TABLE[MsgKind.COMPLETION] = Actor.WORKER`. Add to `__all__`.
  - [x] Add `proposed_ops: list[MemoryOp] = []` to `Result` as an **optional field defaulting to empty** (use `msgspec.field(default_factory=list)`). Do **not** bump `SCHEMA_VERSION` (additive, AD-13). `MemoryOp` is the existing closed union — import/reuse it, don't redefine.
  - [x] Leave the apply dispatch (Task 3) open so Story 3.4 can widen the union to include an `AddFace` op without touching the wire.

- [x] **Task 2: Broker — return the completion to the worker (no Result→core)** (AC: 1)
  - [x] In `broker/service.py:_serve_connection`, after `handle_job_chain` yields its `Result`, emit `Envelope(kind=COMPLETION, src=BROKER, dst=WORKER, body=Completion(ok=result.ok, payload=result.payload, error=result.error), turn_id=env.turn_id)` instead of the `RESULT→CORE` envelope. Keep the existing per-frame resilience (skip bad frame / break on framing error / break on lost peer). The broker still does ZERO pet-domain parsing.
  - [x] `handle_job`/`handle_job_chain` can stay returning a `Result` (simplest, no churn) — the service maps it to a `Completion`. Do NOT move op-parsing into the broker.

- [x] **Task 3: Worker — await the completion, parse ops, emit the Result** (AC: 1, 2)
  - [x] In `worker/worker.py:run_worker`, after writing the `Job`, **stay connected** and `read_frame` the `COMPLETION` (the hub routes it to this WORKER connection). On a clean EOF / read error / `None` (broker gone), build `Result(ok=False, error="…")` so core degrades — do not hang or die silently (the core turn timeout is the backstop, AD-12).
  - [x] On a successful completion, **parse the reply text into `proposed_ops`**: define a single, minimal, testable wire format — a lone fenced ```ops code block containing a JSON array of tagged memory-op objects. Decode it with msgspec into `list[MemoryOp]` (closed/validated — a malformed/unknown op makes the WHOLE block parse fail → `proposed_ops=[]`, reply still delivered). The **user-facing `payload`** is the completion text with that block **removed/absent**. No block → `proposed_ops=[]`, payload = the whole completion (today's behavior, unchanged).
  - [x] Emit `Envelope(kind=RESULT, src=WORKER, dst=CORE, body=Result(ok=…, payload=…, error=…, proposed_ops=…), turn_id=turn_id)` (stamp the worker's own `turn_id`), then close + exit so the reap releases the ≤1-worker bound. Keep the worker LLM-adapter-only (AD-3) — no writes, no creds.
  - [x] **Note (binding):** 4.5 owns the *parse* format; Story 4.4 owns the *prompt* that elicits it. Pick a format that is trivial to assert in a test against a canned completion; flag in Dev Notes that 4.4 may co-adjust it.

- [x] **Task 4: Core — hold `CuratedMemory`, apply proposed ops in the fenced path** (AC: 2)
  - [x] In `Core.__init__`, construct `self.memory = CuratedMemory(memory_root if memory_root is not None else DEFAULT_MEMORY_ROOT)` with an injectable `memory_root=None` param (mirror `history_path`/`faces_path`). Add a thin `Core.apply_memory_op(op)` passthrough to `self.memory.apply_memory_op(op)` (the 4.2-deferred wire passthrough).
  - [x] In `_handle_result`, on the `result.ok` branch, AFTER `_send_reply` + `_push_face(FACE_REPLY)` (so the reply is never blocked by ops), apply `result.proposed_ops`: **cap the count** (a module-level `MAX_PROPOSED_OPS`; reject/log the overflow — no silent truncation), then apply each op in a try/except — a `ValueError`/bad op is **logged and skipped**, never raised into the turn loop (mirror `_record_turn`'s best-effort guard). Dispatch memory-ops to `self.apply_memory_op`; leave a clear seam where 3.4 adds the `AddFace`→`apply_add_face` branch. Then `_record_turn` as today.
  - [x] The failure branch (`_degrade`) is unchanged — a failure `Result` carries no ops.

- [x] **Task 5: Conftest isolation (retro #3, same change)** (AC: 2)
  - [x] Extend the autouse `_isolate_state_checkpoint` fixture to redirect the memory root off real `$HOME`. If `runtime.py` imports `DEFAULT_MEMORY_ROOT` into its namespace, redirect `_runtime.DEFAULT_MEMORY_ROOT` (the name runtime resolves), exactly like `DEFAULT_FACES_PATH`/`DEFAULT_HISTORY_PATH`. Confirm no test touches real `~/.shelldon/memory`.

- [x] **Task 6: Tests** (AC: 1, 2, 3)
  - [x] **AC1 (topology):** a worker that receives a `COMPLETION` emits a `RESULT` with `src=WORKER` to core (update/extend `test_worker_sends_job.py` and `test_broker_bus.py`, which currently assert `src=BROKER` and a fire-and-forget Job). Assert the broker emits `COMPLETION→WORKER` (not `RESULT→CORE`). Assert `turn_id` is preserved end-to-end. Update the `_result_env` helper in `test_turn_fence.py` (`src=Actor.BROKER` → `WORKER`).
  - [x] **AC1 (≤1 + fencing intact):** the end-to-end turn test (`test_end_to_end_turn.py`) still: spawns ≤1 worker, delivers the reply, reacts the face, and fences a late/zombie Result. The worker now does the round-trip — keep the in-process spawner/broker wiring green.
  - [x] **AC2 (apply + reject):** a `Result` with a valid `Remember`/`RewriteAbout` op → core applies it (assert the markdown file appears under the injected memory root) AFTER the reply was sent; an **invalid op** → reply still delivered, no write, turn loop survives, logged; an **oversized** `proposed_ops` (> `MAX_PROPOSED_OPS`) → capped/rejected, reply unaffected. Drive these by putting a crafted `Result` envelope into `core_inbox` for the open turn (no real worker needed) — the apply path is core-only.
  - [x] **AC3 (contract):** round-trip a `Result` with and without `proposed_ops` (default empty) and the new `Completion` body / `COMPLETION` kind (extend `test_contracts_roundtrip.py`); assert `SCHEMA_VERSION` unchanged and every `MsgKind` still has a `ROUTING_TABLE` entry.
  - [x] **Worker parse:** a canned completion containing an ```ops block → `proposed_ops` decodes to the expected `MemoryOp`s and the block is stripped from `payload`; a completion with no block → `proposed_ops=[]`, payload unchanged; a malformed block → `proposed_ops=[]`, full text as payload (reply unaffected).

- [x] **Task 7: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT. **Critical:** the worker now imports `MemoryOp` from `contracts/` (fine — contracts is shared) but must STILL NOT import `core/memory` or any core writer (the worker proposes, it never writes — AD-5). Confirm `core/` stays LLM-free (the parse lives in `worker/`, not `core/`).
  - [x] `uv run pytest -q` → green (the existing suite + new tests; existing topology tests updated, NOT deleted). No network, no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **AD-2 — broker is pure egress/safety boundary; the WORKER parses, never the broker.** "the broker stays a pure egress/safety boundary (no pet-domain parsing — AD-2)." The broker relays text/error via `Completion`; turning a reply into `proposed_ops` is the worker's job. Putting the parse in the broker is the headline AD-2 violation to avoid. [Source: ARCHITECTURE-SPINE.md#AD-2; epics.md#Story 4.5]
- **AD-3 — the worker is the brain adapter (assembles the prompt AND interprets the response).** The symmetry: the same process that builds the prompt (warm libs) interprets the reply into ops. 4.5 builds the "interpret" half; 4.4 builds the "assemble" half (real prompt). The worker dies after emitting its Result (one turn, then RAM reclaimed). [Source: ARCHITECTURE-SPINE.md#AD-3]
- **AD-5 — core is the SOLE writer; workers only propose.** "Workers never write — a `Result` envelope carries *proposed* changes, which core validates and applies." `proposed_ops` IS that carrier; core's fenced apply loop is the validate-and-apply. The worker must not import a core writer. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-6 — memory-ops have fixed arg schemas in `contracts/`; no free-text deltas.** `proposed_ops: list[MemoryOp]` reuses the closed union from 4.2 — the wire carries typed ops, not raw text deltas. [Source: ARCHITECTURE-SPINE.md#AD-6]
- **AD-9 — ≤1 worker in flight; events coalesce into one pending slot.** The worker now lives longer (Job → await completion → Result) but is still exactly one fork per turn; the arbiter (policy) + `ForkServer.worker_in_flight` (mechanical) bounds are unchanged. The reap still releases the bound after the worker exits. A required M0 test. [Source: ARCHITECTURE-SPINE.md#AD-9, #AD-10]
- **AD-12 — `turn_id` fencing; a closed/superseded/late Result is discarded; close is idempotent.** Core fences on `turn_id` only — it does not inspect `src`, so BROKER→WORKER as the Result source needs no fence change. The worker stamps its own `turn_id`; the broker echoes it on the `Completion`. The core turn timeout remains the backstop if the worker never emits. [Source: ARCHITECTURE-SPINE.md#AD-12, shelldon/core/turn.py]
- **AD-13 — additive wire fields are non-breaking; no version bump.** `proposed_ops` defaults to empty; old plain replies are unaffected; `SCHEMA_VERSION` stays 1. [Source: ARCHITECTURE-SPINE.md#AD-13]
- **AD-1 — LLM-free core.** The reply→ops parse lives in `worker/`, not `core/`. Import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1, pyproject.toml#tool.importlinter]

### Design guidance (what to build, minimally)

- **The reshape is small and surgical.** Three real moves: (1) broker emits `COMPLETION→WORKER` instead of `RESULT→CORE`; (2) the worker stops fire-and-forgetting — it reads the completion, parses, and emits `RESULT→CORE`; (3) core grows a `CuratedMemory` + an apply loop in the existing `_handle_result`. Everything else (fence, arbiter, reap, timeout, degrade, history) is untouched.
- **A new hop needs a new kind AND a new body.** `Envelope.__post_init__` enforces kind↔body agreement, so reusing the `Result` body under a `COMPLETION` kind is impossible by design — add `Completion` + `MsgKind.COMPLETION` + routing + the `_KIND_FOR_BODY`/union entries together.
- **Reply first, ops second, best-effort.** Deliver the user-facing reply + face BEFORE applying ops; apply each op guarded (log + skip on failure) so a bad op never blocks or crashes the turn. Cap the op count and log the overflow (no silent truncation — recurring review theme).
- **Pick a dead-simple parse format and keep it in `worker/`.** A single fenced ```ops JSON array, decoded with msgspec into `list[MemoryOp]`, block stripped from the payload. Whole-block reject on any malformed op (the 3.1/3.3/4.2 whole-reject discipline). Flag that Story 4.4 owns the prompt that produces it and may co-adjust the format.
- **Inject the memory root; extend conftest in the same change** (retro #3). Core gets `memory_root=None` like `history_path`.
- **Leave the faces seam open.** Core's op dispatch should be a clean branch point where 3.4 adds `AddFace → apply_add_face` — do not pre-build the face op (that's 3.4), just don't wall it out.

### What 4.5 does NOT do

- **No real prompt assembly / memory injection** — Story 4.4. The worker still gets the prompt text from core verbatim; it does not read history/`about.md`/`DIRECTIVE.md` yet.
- **No designed LLM ops protocol / prompt** — 4.4 writes the prompt that makes the LLM emit the ops block. 4.5 builds + tests the parse against canned completions.
- **No faces op in `proposed_ops`** — Story 3.4 widens the union with `AddFace` and adds the `apply_add_face` dispatch branch (4.5 leaves room).
- **No `capture_learning` / learnings table / dream cycle** — Epic 6.
- **No broker-side parsing of any kind** — AD-2. The broker only relays text/error.
- **No redelivery/supersession sophistication** — a dropped catch-up prompt stays accepted-degraded (Epic 2/later); the turn timeout is the backstop.

### Project Structure Notes

- **Modified:** `shelldon/contracts/__init__.py` (`MsgKind.COMPLETION`, `Completion` struct, `ROUTING_TABLE` + `_KIND_FOR_BODY` + body union + `__all__`; `Result.proposed_ops`); `shelldon/broker/service.py` (emit `COMPLETION→WORKER`); `shelldon/worker/worker.py` (await completion, parse ops, emit `RESULT→CORE`; new parse helper); `shelldon/core/runtime.py` (`self.memory = CuratedMemory(...)`, `memory_root` param, `Core.apply_memory_op` passthrough, op-apply loop + `MAX_PROPOSED_OPS` in `_handle_result`); `tests/conftest.py` (redirect the memory root). 
- **Tests updated (not deleted):** `tests/test_worker_sends_job.py`, `tests/test_broker_bus.py`, `tests/test_turn_fence.py` (the `_result_env` `src`), `tests/test_end_to_end_turn.py`, `tests/test_contracts_roundtrip.py`. **New:** worker-parse tests + core-apply tests (a new `tests/test_proposed_ops.py` is reasonable, or fold into existing files).
- **No new module needed** beyond the parse helper (keep it in `worker/worker.py` or a small `worker/parse.py` if it grows). The curated layer (`core/memory.py`) and the apply path already exist (4.2). `core/` + `contracts/` + `worker/` boundaries unchanged → import-linter KEPT. [Source: ARCHITECTURE-SPINE.md#Structural-Seed]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Inject a `tmp_path` memory root for every test — **never real `$HOME`** (extend the autouse fixture). For core-apply tests, drive a crafted `Result` envelope into `core_inbox` for the open turn (no real worker/broker needed — the apply path is core-only). For the worker round-trip, the end-to-end test already wires a real bus + broker; reuse that harness. Assert: `COMPLETION→WORKER` emission, `RESULT` `src=WORKER`, `turn_id` preserved, ≤1 worker, fence discards a late Result, op applied AFTER the reply, invalid/oversized op rejected without side effects, contract round-trip (with/without `proposed_ops`, new `Completion`/kind), `SCHEMA_VERSION` unchanged. Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: epic-2-retro, epic-3-retro]

### Previous story intelligence (Story 4.2 + Epic 1/2/3)

- **4.2 shipped the apply half** (`CuratedMemory.apply_memory_op`, the closed `MemoryOp` union, atomic writes, path safety, disjoint-writer `DIRECTIVE.md`). 4.5 is the wire that reaches it. The 4.2 owner decisions stand: Unicode-safe filenames, same-name overwrite is intended curation, `episodes.md` is in the write set. [Source: 4-2-curated-markdown-memory-and-memory-ops.md]
- **3.3 shipped `apply_add_face`** and `Core.apply_add_face` already exists — the symmetric face path 3.4 will wire onto this same `proposed_ops` rail. [Source: shelldon/core/runtime.py:333, shelldon/core/faces.py]
- **Recurring review themes to pre-empt:** guard inputs (cap op count, validate each op, whole-reject a malformed parse); never silently swallow (log a skipped/oversized op, never silently truncate); never let best-effort bookkeeping crash the turn loop (mirror `_record_turn`'s guard); value-not-truthiness asserts; share test helpers via conftest; proactive `$HOME` isolation in the SAME change. [Source: epic-3-retro-2026-06-17.md, 4-1/4-2 Review Findings]
- **The fire-and-forget worker + RESULT→CORE was a deliberate M0 shape (1.5/1.8).** This story is the first to reshape it; keep the change surgical and the existing topology tests updated rather than rewritten, so the ≤1/fencing guarantees stay provably intact.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 4 / Story 4.5 (this story, full ACs + topology decision); #Story 4.2 (apply half — done); #Story 4.4 (prompt assembly — consumes/produces the ops protocol); #Story 3.4 (faces self-modify — rides this wire, adds AddFace); #Story 1.5 (fork-server worker), #Story 1.8 (end-to-end turn — the topology being reshaped)]
- [Source: ARCHITECTURE-SPINE.md#AD-2 (broker pure egress — why worker parses), #AD-3 (worker brain adapter — assemble+interpret symmetry), #AD-5 (core sole writer, workers propose), #AD-6 (fixed-schema memory-ops), #AD-9 (≤1 worker), #AD-12 (turn_id fencing), #AD-13 (non-breaking wire add), #AD-1 (LLM-free core)]
- [Source: shelldon/broker/service.py (the Result→core emission that moves), shelldon/broker/broker.py (handle_job_chain returns a Result), shelldon/worker/worker.py (fire-and-forget Job — the worker reshape), shelldon/core/runtime.py (_handle_result fenced path; apply_add_face precedent; CuratedMemory wiring point), shelldon/core/turn.py (TurnFence — unchanged), shelldon/core/bus/server.py (kind-based routing + registration), shelldon/contracts/__init__.py (MsgKind/Envelope/Result/_KIND_FOR_BODY/ROUTING_TABLE; the closed MemoryOp union)]
- [Source: tests/conftest.py (_isolate_state_checkpoint autouse fixture to extend); tests/test_worker_sends_job.py, tests/test_broker_bus.py, tests/test_turn_fence.py, tests/test_end_to_end_turn.py, tests/test_contracts_roundtrip.py (topology/contract tests to update)]
- [Source: _bmad-output/implementation-artifacts/4-2-curated-markdown-memory-and-memory-ops.md (owner decisions 2026-06-17: worker-emits-Result topology, 4.2/4.5 split; Unicode filenames; episodes.md in the write set)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- **The reshape surfaced a real wedge risk (caught by the suite hanging, not asserting).** The old `test_worker_sends_job` did `await run_worker(...)` against a stub broker; with the worker now awaiting a Completion, that hung forever. More importantly it exposed that a **silent/crashed broker** produces NO EOF on the worker's hub connection — the worker would block indefinitely, never reap, and stick the ≤1 bound (AD-9). Fix: a bounded `asyncio.wait_for` on the completion read (`_COMPLETION_TIMEOUT_S`, generous + test-injectable) → on timeout the worker emits a failure Result and exits. This was NOT explicitly in the story (which assumed EOF); it's required for the "never hang / never wedge" guarantee.
- Final: `uv run pytest -q` → 243 passed (was 230); `uv run lint-imports` → both contracts KEPT.

### Implementation Plan

Three surgical moves + the apply side, exactly as scoped:

1. **contracts** — added `MsgKind.COMPLETION` + a `Completion(ok/payload/error)` body (broker→worker carrier), registered in `_KIND_FOR_BODY`/the `Envelope.body` union/`ROUTING_TABLE` (`COMPLETION→WORKER`)/`__all__`. Added `Result.proposed_ops: list[MemoryOp] = []` (additive, no `SCHEMA_VERSION` bump — AD-13). **Moved** the `MemoryOp` union above `Result` so the field can reference it (no `__future__` annotations churn).
2. **broker/service.py** — `_serve_connection` now emits `COMPLETION→WORKER` (mapped from the chain's `Result`) instead of `RESULT→CORE`. Zero pet-domain parsing stays in the broker (AD-2). `handle_job_chain` is unchanged (still returns a `Result` internally).
3. **worker/worker.py** — `run_worker` stops fire-and-forgetting: send Job → bounded-await the `Completion` → `parse_reply` (a lone fenced ```ops JSON array → `list[MemoryOp]`, whole-reject on malformed, block stripped from the user payload) → emit `RESULT→CORE` (`src=WORKER`, stamped `turn_id`) → exit. A missing/late/bad completion → failure Result (core degrades).
4. **core/runtime.py** — Core now holds `self.memory = CuratedMemory(memory_root)` (injectable) + a thin `apply_memory_op` passthrough. `_handle_result` applies `proposed_ops` AFTER the reply+face via `_apply_proposed_ops`: count capped at `MAX_PROPOSED_OPS` (overflow dropped w/ a warning), each op applied guarded (invalid → log+skip, never crashes the turn). A clear seam marks where Story 3.4 adds the `AddFace→apply_add_face` branch.
5. **conftest** — redirect `_runtime.DEFAULT_MEMORY_ROOT` (and the memory module's) off real `$HOME` in the same change (retro #3).

### Completion Notes List

- **All 3 ACs satisfied.** AC1: broker returns `Completion→WORKER`, worker emits `Result(src=WORKER)→core`, `turn_id` fencing + ≤1-worker bound intact (end-to-end suite green, including the coalesce + late-Result-fenced tests). AC2: core applies ops via `apply_memory_op` after the reply; invalid op skipped, oversized capped, reply never affected. AC3: `proposed_ops` is an additive empty-default field, `SCHEMA_VERSION` unchanged, round-trip covers with/without ops + the `Completion` kind/body.
- **Scope held:** no prompt assembly/memory injection (4.4); the LLM ops protocol is tested against canned completions (4.4 owns the prompt); no faces op (3.4 — the dispatch seam is left open); no `capture_learning` (Epic 6); broker does no parsing (AD-2).
- **Topology tests updated, not deleted** — `test_worker_sends_job`, `test_broker_bus`, `test_broker_service`, `test_broker_chain_fallback`, `test_turn_fence` now assert the new wire (Completion→worker, Result src=WORKER); the ≤1/fencing guarantees stay provably intact.
- **Design note for review:** the `_COMPLETION_TIMEOUT_S` backstop is the one addition beyond the story text — flagged above. The reply→ops format (fenced ```ops block) is provisional and co-owned with Story 4.4.

### File List

- **Modified:** `shelldon/contracts/__init__.py` — `MsgKind.COMPLETION`, `Completion` struct, `Result.proposed_ops`, `MemoryOp` moved above `Result`, routing/union/`_KIND_FOR_BODY`/`__all__`.
- **Modified:** `shelldon/broker/service.py` — emit `COMPLETION→WORKER` (was `RESULT→CORE`); docstring.
- **Modified:** `shelldon/worker/worker.py` — await completion + `parse_reply` + emit Result; `_COMPLETION_TIMEOUT_S` backstop.
- **Modified:** `shelldon/core/runtime.py` — `CuratedMemory` wiring + `memory_root` param, `apply_memory_op` passthrough, `_apply_proposed_ops` + `MAX_PROPOSED_OPS`, apply call in `_handle_result`.
- **Modified:** `tests/conftest.py` — redirect `_runtime.DEFAULT_MEMORY_ROOT`.
- **Modified (tests updated for the new wire):** `tests/test_worker_sends_job.py`, `tests/test_broker_bus.py`, `tests/test_broker_service.py`, `tests/test_broker_chain_fallback.py`, `tests/test_turn_fence.py`, `tests/test_contracts_roundtrip.py`.
- **Added:** `tests/test_proposed_ops.py` — worker `parse_reply` + core fenced-apply tests.

### Change Log

- 2026-06-17 — Implemented Story 4.5: the worker-proposes-ops write-back wire. Broker returns a `Completion` to the worker; the worker parses its reply into `Result.proposed_ops` and emits `Result→core`; core validates+applies the ops via the 4.2 apply path (sole writer), after the reply, capped + guarded. `turn_id` fencing + ≤1-worker bound preserved; broker stays pure egress. Suite 243 green; import contracts KEPT.
- 2026-06-17 — Addressed code review findings — 4 patches resolved (4 lower-priority items deferred with rationale): multi-block ops parse (no leak), `OSError`/`EOFError` caught on the completion read, AC2 reply-before-op ordering now test-enforced, and the failure-skips-ops path actually exercised. +2 tests; suite 244 green; contracts KEPT.

### Review Findings

- [x] [Review][Patch] Multiple ops blocks: second block leaks verbatim into user-facing payload [shelldon/worker/worker.py, parse_reply]
  - **Resolved:** `parse_reply` now `finditer`s ALL ```ops blocks, accumulates ops, and strips every parsed block (reverse-order splice). A malformed block is left in place (visible, never silently swallowed). Added `test_parse_reply_handles_multiple_blocks_without_leaking`.
- [x] [Review][Patch] `_result_from_broker` does not catch `OSError` from `read_frame` on hard transport failure [shelldon/worker/worker.py, _result_from_broker]
  - **Resolved:** the completion-read except now also catches `OSError`/`EOFError` (covers `IncompleteReadError` + connection resets) → returns a failure Result so core degrades; the worker task never crashes.
- [x] [Review][Patch] "ops applied AFTER reply" ordering not enforced by test — AC2 guarantee unverified [tests/test_proposed_ops.py, test_core_applies_valid_op_after_reply]
  - **Resolved:** the test now spies `_send_reply` + `memory.apply_memory_op` into an order list and asserts `["reply", "apply"]` — the AC2 reply-before-op guarantee is now verified.
- [x] [Review][Patch] `test_core_failure_result_applies_no_ops` uses empty ops — "failure result skips ops" path never exercised [tests/test_proposed_ops.py, test_core_failure_result_applies_no_ops]
  - **Resolved:** renamed to `test_core_failure_result_skips_ops`; it now passes a failure Result that DOES carry an op and asserts nothing is written — the failure-branch op-skip is actually exercised.
- [x] [Review][Defer] `write_frame` for outbound Result (worker→core) has no timeout — pre-existing write path issue [shelldon/worker/worker.py, run_worker] — deferred, pre-existing
- [x] [Review][Defer] `parse_reply` `.strip()` destroys intentional leading/trailing whitespace — low risk until 4.4 defines prompt format [shelldon/worker/worker.py, parse_reply] — deferred, pre-existing
- [x] [Review][Defer] COMPLETION dropped at hub + 90s `worker_in_flight` freeze asymmetry (core degrades at 30s, worker times out at 120s) — deferred, known design tradeoff
- [x] [Review][Defer] ops block with no `\n` after opening fence silently unmatched (no warning) — deferred, extremely unlikely LLM output; 4.4 owns the prompt format
