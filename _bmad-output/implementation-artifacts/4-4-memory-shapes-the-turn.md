---
baseline_commit: f64df1f
---

# Story 4.4: Memory shapes the turn

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want what the pet knows to actually change how it replies,
so that memory is real, not just stored — the worker assembles each prompt from the durable memory it can read (DIRECTIVE + about + recent history + recall), proving CAP-6 (a fact from an earlier turn shows up in a later reply).

## Acceptance Criteria

1. **The worker assembles the prompt from memory before proxying to the broker (AD-3 / AD-6 retrieval order):** Given stored history and curated memory, when a worker runs a turn, then it builds the broker `Job` payload from — in this exact order (AD-6 line 100) — **`DIRECTIVE.md` first (authoritative, if present)**, then **`about.md`** (the bot's self-summary), then a **recent conversation window** (last *N* turns from sqlite, oldest→newest), then **relevant FTS5 recall** (top *k* history matches for the current message, de-duplicated against the recent window), then the **current owner message** last. The worker reads everything through the **read-only** handles built in 4.1/4.2 (`history.open_readonly(...)`, `CuratedMemory(...).read_about()/read_directive()`) — it never writes, and it never reads `vault/` (AD-6). The assembly core is a **pure function** (data in → prompt string out) so it is unit-tested without I/O.

2. **A fact from an earlier turn is demonstrably reflected in a later, related reply (CAP-6 success):** Given a fact established in an earlier turn (recorded to history by core's `record_turn`, or written to `about.md`/`facts/` via a memory-op), when a later related turn runs against a **fake/stub broker that echoes its received prompt**, then the assembled prompt handed to the broker **contains that earlier fact** (via the recent window or FTS5 recall) — proving the memory actually reaches the brain. (We assert on the *assembled prompt*, not on a real LLM's wording — a real model is non-deterministic; the verifiable, non-flaky claim is "the fact was in the prompt the worker sent.")

3. **Assembly is best-effort and never crashes or wedges the turn (resilience parity with 4.1/4.5):** Given memory that is empty, partially missing, or unreadable, when the worker assembles, then a missing `DIRECTIVE.md`/`about.md` (read accessors return `None`) **omits that section** (no placeholder noise), an empty history yields **no recent/recall section**, and any read failure (sqlite open error, an **FTS5 `MATCH` syntax error from raw owner punctuation**, a decode error) is **caught and degrades to a smaller prompt** (worst case: just the current owner message) — it is **logged, never raised** into the turn (mirrors `_record_turn`/the proposed-ops apply guard). The recent window and recall counts are **bounded** (a configured cap, 512MB-conscious — NFR), never unbounded.

> **Scope seam (binding):** 4.4 builds the **prompt-assembly layer** — the worker reading DIRECTIVE/about/history (read-only) and composing the broker `Job` from them in the AD-6 order, plus the **system instruction** that tells the model how to reply and how to emit the `​```ops` block (the format 4.5's `parse_reply` already consumes). It does **NOT** build: **vault surfacing** — surfacing `vault/` secrets into a prompt is **broker-gated** (AD-6) and a separate mechanism (the worker can't read `vault/`; it would *request*, the broker would *inject at egress*). 4.3 built the broker's `surface_vault` authority but left it unwired; the epic's 4.4 ACs are memory-injection only, so vault-surfacing is **deferred to a follow-on story** (see Open Questions). Also OUT: the **running summary / window compaction** (AD-7 — that's the dream cycle, Epic 6); the **`learnings` table / `capture_learning`** (Epic 6); any **change to the ≤1-worker bound, fencing, the bus, the broker egress, or core's sole-writer apply path** (4.5). The single biggest mistake is rebuilding vault surfacing or a summarizer here — 4.4 is "read what we already store and put it in the prompt, in order, safely."

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent) — verified against the code:**
> - **The worker already owns prompt assembly per the spine, but does NONE yet.** `worker.run_worker(socket_path, turn_id, prompt)` sends `Job(payload=prompt)` with the raw owner text core handed in; its docstring says verbatim *"Real prompt assembly (history + memory) is Story 4.4; here the prompt is whatever core handed in."* 4.4 makes the worker assemble before the `Job`. [Source: shelldon/worker/worker.py:run_worker, :100-115; ARCHITECTURE-SPINE.md AD-3 line 81, line 166 "worker: prompt assembly"]
> - **The read-only handles are already built (4.1/4.2) and are exactly the worker's read surface.** History: `history.open_readonly(path) -> HistoryReader` with `.recent(n)` (chronological) and `.search(query, n)` (FTS5, most-relevant). Memory: `CuratedMemory(root).read_about()` and `.read_directive()` (both return `str | None`, gated on `path.is_file()`). The worker reads through these — never the writer classes. [Source: shelldon/core/history.py:open_readonly/HistoryReader/_recent/_search, shelldon/core/memory.py:read_about/read_directive]
> - **`read_directive()` was built FOR this story.** Its docstring: *"Story 4.4 injects it first as authoritative context."* `read_about()`: *"The read accessor Story 4.4 will inject into prompts."* [Source: shelldon/core/memory.py:138-149]
> - **The worker is forked and currently gets only `(socket_path, turn_id, prompt)`.** To read memory/history it needs the `memory_root` + `history_path`. Mirror 4.3's precedent: the `ForkServer` already carries injected config (`worker_uid`/`worker_gid`) into the child via `_default_spawn`/`_os_fork_spawn` — thread `memory_root`/`history_path` the same way (defaulting to `DEFAULT_MEMORY_ROOT`/`DEFAULT_HISTORY_PATH`). The in-process test seam (`Spawns.spawn`) and `app.py` must pass them too. [Source: shelldon/worker/forkserver.py:_default_spawn/_os_fork_spawn, tests/test_end_to_end_turn.py:Spawns, shelldon/app.py]
> - **`parse_reply` already consumes the `​```ops` block this story's prompt elicits.** 4.5 owns the PARSE; 4.4 owns the system instruction that asks the model for `(reply + optional ops block)`. Keep the format the regex expects: a fenced ```` ```ops `` block with a newline after the fence (`_OPS_BLOCK_RE`). [Source: shelldon/worker/worker.py:_OPS_BLOCK_RE/parse_reply]
> - **Conftest already redirects `DEFAULT_MEMORY_ROOT`/`DEFAULT_HISTORY_PATH` off real `$HOME`.** New worker reads are isolated for free; extend only if a new default path is introduced. [Source: tests/conftest.py:_isolate_state_checkpoint]

- [x] **Task 1: Pure prompt-assembly function (the testable core)** (AC: 1, 3)
  - [x] `assemble_prompt(owner_message, *, directive, about, recent, recall, system=...) -> str` in new `shelldon/worker/prompt.py`. Pure; composes in the AD-6 order **system → DIRECTIVE → about → recent → recall → current**; None/blank sections omitted; current message always last.
  - [x] `SYSTEM_INSTRUCTION` defined — brief pet identity + "always speak a reply first" + the optional `​```ops` fenced JSON example (`"type"`-tagged, matching `parse_reply`/`_OPS_DECODER`). The empty-payload guard (deferred #138) is folded as "always say something back first."
  - [x] Recent window rendered oldest→newest with role labels; recall as a "Things you remember" block. Labels minimal/stable (tests assert membership).

- [x] **Task 2: Context gather (the I/O wrapper) + recall safety** (AC: 1, 3)
  - [x] `gather_context(memory_root, history_path, owner_message, *, recent_n, recall_k) -> dict` opens the read-only handles (`open_readonly`, `CuratedMemory.read_directive/read_about`), reads recent + FTS5 recall, de-dups recall vs recent by row `id`, closes the reader. Bounded (`RECENT_TURNS`/`RECALL_LIMIT`, term cap).
  - [x] **FTS5 safety:** `_fts_query` tokenizes to bare `\w+` terms, quotes each, ORs them (defuses operators/punctuation), caps term count; `search` also wrapped in `try/except sqlite3.OperationalError -> []`. Tested with a hostile message.
  - [x] **Fail-soft:** memory read guarded (`OSError`), history open/read guarded (`sqlite3.Error`/`OSError`) — any failure logs + degrades to empty context (→ system + owner message). Mirrors `_record_turn`.

- [x] **Task 3: Wire the worker to assemble before the Job** (AC: 1, 2)
  - [x] `run_worker(..., *, memory_root=None, history_path=None, assemble=None)` — default `assemble` = `build_prompt` bound to the roots; threads `memory_root`/`history_path` through `ForkServer` → `_default_spawn` → `_os_fork_spawn` → child (the 4.3 uid/gid precedent). `app.py` passes the configured `memory_root`. The `assemble` seam lets lifecycle tests inject identity.
  - [x] The Job payload is now the assembled prompt; the Completion→`parse_reply`→Result half (4.5) is unchanged.
  - [x] No new write path, no vault read — read-only handles only; core's apply path untouched.

- [x] **Task 4: Tests** (AC: 1, 2, 3)
  - [x] **Pure assembly** (`test_prompt_assembly.py`): order, section omission, blank-omission, system present, current-last, recall de-dup, `_fts_query` quoting/None.
  - [x] **CAP-6 (AC2)** (`test_end_to_end_turn.py::test_cap6_fact_from_earlier_turn_reaches_later_prompt`): turn 1 states a fact → recorded; turn 2 (real assembler + `RecordingProvider`) → the assembled prompt the broker received **contains the fact**. Asserts on the prompt, not model wording.
  - [x] **Resilience (AC3):** missing history degrades (empty windows), FTS-hostile message no crash, missing memory → directive/about None, recall surfaces beyond the recent window.
  - [x] **Wiring/regression:** lifecycle/worker tests inject identity assembly (`Spawns` default `_passthrough_worker`, `late_worker`, `test_worker_sends_job`, app-root smoke) so they stay about fencing/coalescing — all green.

- [x] **Task 5: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (`worker/prompt.py` imports only `core.history`/`core.memory` read APIs + stdlib; no provider SDK).
  - [x] `uv run pytest -q` → **306 passed, 3 skipped** (the Linux fork/uid gates, unchanged), 3 deselected (live smoke). No network, no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **AD-6 retrieval order is literal (line 100):** *"inject `DIRECTIVE.md` (authoritative, first) + `about.md` + the recent conversation window (from sqlite) + … FTS5 over history. Workers read history read-only and read the markdown tree minus `vault/`."* Build exactly that order. DIRECTIVE is the owner's constitution — first and authoritative. [Source: ARCHITECTURE-SPINE.md AD-6 line 99-100]
- **AD-3 — the worker assembles, with warm libs, then proxies to the broker:** *"the worker assembles the prompt with warm libs but proxies the authenticated call to the broker."* Assembly is the worker's job, not core's — core stays the orchestrator + sole writer. [Source: ARCHITECTURE-SPINE.md AD-3 line 81, line 166]
- **AD-5 — workers never write; read-only handles only.** Use `open_readonly`/`read_about`/`read_directive`. The worker proposing memory changes is the 4.5 `Result.proposed_ops` path (unchanged here). [Source: ARCHITECTURE-SPINE.md AD-5 line 91]
- **AD-6 vault is OS-enforced and broker-gated for surfacing — OUT of 4.4.** The worker physically cannot read `vault/` (4.3 uid drop); surfacing secrets is a broker-gated egress decision, a separate mechanism. Do not wire it here. [Source: ARCHITECTURE-SPINE.md AD-6 line 100, Story 4.3 scope note]
- **AD-1 — LLM-free core stays intact.** Assembly lives in `worker/` (the brain adapter), not `core/`. It is pure string composition (no provider SDK) — the import-linter `core` contract is unaffected; just don't pull a provider lib into `worker/prompt.py`. [Source: pyproject.toml#importlinter]

### Design guidance (what to build, minimally)

- **Split pure-vs-I/O like the rest of the codebase.** `assemble_prompt(...)` is pure (mirrors `parse_reply`); `gather_context(...)` wraps the read-only opens (mirrors `_result_from_broker`). The pure function carries the test weight; the wrapper carries the fail-soft.
- **Assert on the assembled prompt, never on model output.** CAP-6's verifiable claim is "the fact reached the brain" = "the fact is in the `Job` payload." Use a stub broker that echoes the payload back (the existing fakes in `test_end_to_end_turn.py` are the pattern). A real-LLM assertion would be flaky.
- **Bound everything.** `recent_n` (e.g. ~10 turns) and `recall_k` (e.g. ~5) are configured module constants (tests inject small values). 512MB box → never assemble an unbounded backlog (NFR). De-dupe recall against the recent window so the same row isn't shown twice.
- **Fail soft, loudly logged.** Every read can fail (missing file, locked sqlite, FTS5 syntax). Each failure degrades the prompt and logs — never raises into the turn (the turn must still complete/degrade gracefully, like 4.1's guarded history write). The FTS5-from-raw-text crash is the easy-to-miss one — guard it explicitly.
- **The system instruction is the only LLM-facing copy.** Keep it short and own the `​```ops` format here (4.5 parses it; 4.4 elicits it). This is where deferred-work item "ensure the prompt asks for both a reply AND an ops block" lands (see below).

### Previous story intelligence (4.1 / 4.2 / 4.5 / 4.3)

- **4.1** built `HistoryReader`/`open_readonly` (`mode=ro`) + `recent`/`search` — the worker's read surface. History writes are guarded best-effort (a sqlite failure never crashes the turn) — copy that discipline for reads. [Source: shelldon/core/history.py]
- **4.2** built `read_about`/`read_directive` (return `None` when absent, `path.is_file()` gated — a fixed review finding) and the disjoint-writer `DIRECTIVE.md`. Assembly must tolerate `None` from both. [Source: shelldon/core/memory.py]
- **4.5** built the `parse_reply` ↔ `​```ops` contract and the worker's Job→Completion→Result flow. 4.4 only changes what goes INTO the `Job` payload; the parse/Result half is untouched. Deferred items flagged "revisit in 4.4": `parse_reply.strip()` whitespace (line 70), ops-block-no-newline (line 72), **all-ops reply → empty payload (line 138 — fold a small guard: the system prompt should always ask for a spoken reply too)**. [Source: deferred-work.md:70,72,138]
- **4.3** threaded injected config (`worker_uid`/`worker_gid`) into the fork child via `ForkServer` → `_default_spawn` → `_os_fork_spawn` → child. **Mirror that exact pattern** to thread `memory_root`/`history_path` to `run_worker`. 4.3 also built (unwired) the broker `surface_vault` authority that a future vault-surfacing story consumes. [Source: shelldon/worker/forkserver.py, shelldon/broker/vault.py]
- **Recurring review themes to pre-empt:** best-effort reads never crash the turn (4.1); never fake green / assert real content not truthiness; guard inputs (the FTS5 query); never silently swallow (log every degraded read); bounded/no-unbounded-backlog; `$HOME` isolation already covered by conftest. [Source: epic-3/epic-4 review findings]

### Testing standards

- `pytest` + `pytest-asyncio`. The pure `assemble_prompt` is plain unit tests (order, omission, de-dup, system instruction present). The CAP-6 test reuses the in-process harness with an **echo-the-payload** stub broker. Resilience tests inject empty/missing/failing reads. Wiring regression = the existing `test_end_to_end_turn.py` harness with seeded roots passed through `Spawns`. Inject memory/history roots via the conftest `tmp_path` fixtures; never touch real `$HOME`. Before done: `uv run lint-imports` (KEPT) + `uv run pytest -q` (green). [Source: tests/conftest.py, tests/test_end_to_end_turn.py, tests/test_proposed_ops.py]

### Project Structure Notes

- **New:** `shelldon/worker/prompt.py` (`assemble_prompt` + `gather_context` + the bounded-count constants + the system instruction), `tests/test_prompt_assembly.py` (pure + resilience), and a CAP-6 test (extend `test_end_to_end_turn.py` or a new `tests/test_memory_in_turn.py`).
- **Modified:** `shelldon/worker/worker.py` (assemble before the `Job`), `shelldon/worker/forkserver.py` (carry `memory_root`/`history_path` into the child), `shelldon/app.py` (pass the roots into `ForkServer`), the in-process `Spawns` seam in `tests/test_end_to_end_turn.py`.
- **Boundaries:** assembly is in `worker/` (AD-3), pure-string (AD-1 safe). Read-only handles only (AD-5). No `vault/`, no summarizer, no `learnings`. Import-linter KEPT.

### What 4.4 does NOT do

- **No vault surfacing** — broker-gated, separate mechanism (4.3 built the authority; a follow-on story wires worker-request → broker-inject-at-egress). See Open Questions.
- **No running summary / window compaction** — AD-7 working-window summary is the dream cycle (Epic 6). 4.4 sends a bounded raw recent window.
- **No `learnings` / `capture_learning` / dream cycle** — Epic 6.
- **No change to ≤1-worker, fencing, the bus, broker egress, or core's apply path.**

### Open questions for the owner (raised, not blocking)

1. **Vault surfacing scope.** Story 4.3's note said *"4.4 wires the worker's surface-request + the broker's injection at egress,"* but the epic's **4.4 ACs are memory-injection only** (DIRECTIVE/about/history). CAP-6 success needs only memory injection — vault is owner-secrets, a different capability. **Recommendation:** keep 4.4 = memory injection (this story), and split vault-surfacing into a new story (e.g. **4.6**, or fold into Epic 6 where vault *promotions* already live) so 4.4 doesn't become another "largest story." Confirm.
2. **Which deferred items to fold.** `deferred-work.md` flags three "revisit in 4.4" items (70 whitespace-strip, 72 ops-fence-newline, 138 empty-payload). **Recommendation:** fold only **138** (a tiny guard + the system instruction asking for a spoken reply) since 4.4 owns the eliciting prompt; re-defer 70/72 (cosmetic, unlikely from a well-prompted model). The face-validation gaps (134/137) become live once the LLM proposes faces — fold the cheap size guards or re-defer with a reason.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 4.4 (this story); #Story 4.1/4.2 (read APIs); #Story 4.5 (the ops wire); #Epic 6 (dream cycle — summary/learnings/vault promotion)]
- [Source: ARCHITECTURE-SPINE.md AD-6 (retrieval order, lines 99-100), AD-3 (worker assembles, line 81/166), AD-5 (read-only handles, line 91), AD-1 (LLM-free core), CAP-6 (line 218)]
- [Source: shelldon/worker/worker.py (run_worker — assemble before the Job; parse_reply/_OPS_BLOCK_RE — the ops format to elicit), shelldon/core/history.py (open_readonly/HistoryReader.recent/search), shelldon/core/memory.py (read_about/read_directive), shelldon/worker/forkserver.py (the uid/gid threading precedent to mirror), shelldon/app.py (passes memory_root)]
- [Source: tests/test_end_to_end_turn.py (in-process harness + Spawns seam + echo-broker fakes), tests/conftest.py ($HOME isolation), deferred-work.md (lines 70/72/134/137/138 — items flagged for 4.4)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- None. The one design ripple (the worker now assembling changes what the fake providers echo in lifecycle tests) was handled by the `assemble` seam: lifecycle/worker tests inject identity assembly, so their fencing/coalescing assertions stay valid; only the new CAP-6 test uses the real assembler.

### Completion Notes List

- **Worker assembles (AD-3/AD-6).** New `shelldon/worker/prompt.py`: pure `assemble_prompt` (system → DIRECTIVE → about → recent → recall → current, None/blank omitted) + `gather_context` (read-only opens, FTS5 recall de-duped vs the recent window, fail-soft) + `build_prompt` (the default assembler). Order proven by index assertions.
- **Recall safety.** `_fts_query` quotes/ORs bare `\w+` terms (caps count) so raw owner punctuation/operators can't make `MATCH` raise; `search` is also `try/except`-guarded. Resilience covers missing history (read-only open of a non-existent db), FTS-hostile input, and missing memory.
- **CAP-6 proven on the prompt, not the model.** The headline test seeds a fact in turn 1, then asserts turn 2's assembled prompt (captured via `RecordingProvider`) contains it — verifiable + non-flaky.
- **Wiring mirrors 4.3.** `memory_root`/`history_path` thread `ForkServer` → `_default_spawn` → `_os_fork_spawn` → `run_worker`, exactly like 4.3's uid/gid. `app.py` passes the configured `memory_root`. The `assemble` seam keeps lifecycle tests decoupled from prompt content.
- **Scope held.** No vault surfacing (broker-gated, deferred — see Open Questions), no summarizer, no `learnings`/dream cycle, no change to ≤1/fencing/broker egress/core apply path. The empty-payload guard (deferred #138) is folded into the system instruction ("always say something back first").
- **Verify:** `lint-imports` 2 kept / 0 broken; `pytest -q` 306 passed, 3 skipped, 3 deselected. No network, no real `$HOME`.

### File List

- **New:** `shelldon/worker/prompt.py`, `tests/test_prompt_assembly.py`
- **Modified:** `shelldon/worker/worker.py` (assemble before the Job + `assemble` seam), `shelldon/worker/forkserver.py` (thread `memory_root`/`history_path` to the child), `shelldon/app.py` (pass `memory_root` into the ForkServer), `tests/test_end_to_end_turn.py` (identity-assembly `Spawns` default + `RecordingProvider` + the CAP-6 test), `tests/test_worker_sends_job.py` (identity assemble), `tests/test_app_root.py` (identity assemble in the smoke), `tests/conftest.py` (redirect `worker.prompt` default paths off real `$HOME` — review fix)

## Review Findings

- [x] [Review][Patch] CAP-6 test false positive + broken isolation. **Both fixed + verified.** (1) The test now seeds a unique token (`Cassandra-x9f3`, asserted absent from `SYSTEM_INSTRUCTION`) and asserts on the prompt with the system instruction **stripped**, so the example can't mask a broken recall — confirmed by a negative check (bypassing assembly now makes the test FAIL). (2) conftest now redirects `shelldon.worker.prompt.DEFAULT_MEMORY_ROOT`/`DEFAULT_HISTORY_PATH` to the same tmp paths core uses — in-process workers no longer read real `~/.shelldon` (the "patch each importer" pattern). [tests/test_end_to_end_turn.py CAP-6, tests/conftest.py]
- [x] [Review][Patch] `UnicodeDecodeError` escaping the guard — **fixed.** `gather_context`'s memory read now catches `(OSError, UnicodeError)` (UnicodeError is a ValueError, not OSError), so a corrupt `about.md`/`DIRECTIVE.md` degrades instead of raising (AC3). Added `test_gather_corrupt_about_degrades_not_raises`. [shelldon/worker/prompt.py]
- [x] [Review][Defer] FTS implicit safety invariant: `_fts_query` safety relies on `\w+` never matching FTS metacharacters; loosening the regex later would silently break injection safety [`shelldon/worker/prompt.py:53-62`] — deferred, pre-existing
- [x] [Review][Defer] FTS common-word recall noise: 32-term OR query without stopword filtering; common words (`is`, `my`) match nearly every row, making recall noisy — deferred, pre-existing

## Change Log

| Date       | Change                                                                 |
|------------|------------------------------------------------------------------------|
| 2026-06-18 | Story 4.4 implemented: the worker assembles each prompt from memory (DIRECTIVE + about + recent history + FTS5 recall, AD-6 order) via read-only handles, fail-soft. CAP-6 proven (an earlier fact reaches a later prompt). +13 tests; suite 306 passed / 3 skipped, contracts kept. Status → review. |
| 2026-06-18 | Addressed code-review findings — 2 [Patch] fixes: (1) CAP-6 false positive + `$HOME` isolation breach (unique token + system-instruction-stripped assertion, verified by a negative check; conftest now redirects `worker.prompt` default paths); (2) `UnicodeDecodeError` now caught so a corrupt memory file degrades (AC3). 2 [Defer] FTS items left deferred. +1 test; suite 307 passed / 3 skipped, contracts kept. |
