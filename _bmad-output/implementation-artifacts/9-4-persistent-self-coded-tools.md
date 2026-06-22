---
baseline_commit: 1e638f2db1eb0cc403f927c3486920cdc2562d17
---

# Story 9.4: Persistent self-coded tools

Status: done

## Story

As the owner,
I want shelldon to write a new tool plus its test, get my approval, and have it live next turn,
so that the pet grows its own capabilities over time (v1's self-coding magic).

## Scope decisions (locked with Elliot 2026-06-22)

1. **FREE once promoted** — a self-coded tool's CALLS run inline (FREE-tier) after promotion. The safety boundary is the one-time review at promotion (the gate + the owner approving the CODE); no per-call tap. (Per-call RISKY gating was the alternative — rejected for friction.)
2. **Gate runs in core as an async subprocess** — core stages the files and runs the gate via `asyncio.create_subprocess_exec` (bounded), off the turn-critical path. Core never imports an LLM (LLM-free contract holds). (A dedicated fork was the alternative — rejected for plumbing.)
3. **`ProposeTool` rides the existing ops-block wire** (Story 4.5 `parse_reply` → `proposed_ops`) — the model emits it as a `propose_tool` op in its reply, exactly like `remember`/`add_face`. NO worker-loop change, no new function-call tool.

## Acceptance Criteria

### AC1 — The brain proposes a tool; it is staged and gated (never live yet)

**Given** the model emits a `propose_tool` op (`ProposeTool(name, code, test)`) in its reply
**When** core processes it
**Then** core writes the tool module + its test to a STAGING dir (`<workspace>/tools-staging/`), NOT the live dir, and runs a local gate: `pytest` on the staged test (bounded subprocess) PLUS an import check that rejects the tool if it imports an LLM SDK or `shelldon.core` (the LLM-free-core invariant, mirroring the import-linter forbidden set)
**And** the gate runs off the turn loop (async subprocess) and never blocks core; a gate that times out is treated as a fail

### AC2 — A passing gate asks for owner approval; a failing one is rejected

**Given** a staged tool
**When** the gate PASSES
**Then** core parks a pending PROMOTION keyed by turn id and sends the owner the Story 9.3 inline Approve/Deny keyboard ("I wrote a tool `name` and it passed its test — add it?")
**When** the gate FAILS (test failure, import-check rejection, or timeout)
**Then** the staged files are discarded, the owner gets a brief note with the failure reason, and the tool is NEVER promoted

### AC3 — Approval promotes the tool; it is live and callable next turn

**Given** a passed, parked tool promotion
**When** the owner taps Approve
**Then** core moves the staged files to the LIVE tools dir (`<workspace>/tools/`) and confirms ("`name` is live")
**When** the owner taps Deny
**Then** the staged files are discarded, confirmed ("discarded `name`")
**And** an expired/unknown promotion decision is dropped, logged, never promoted (reuses the 9.3 expiry discipline)

### AC4 — A promoted tool is discovered by the next fresh worker, no restart

**Given** a promoted tool in the live dir
**When** the next turn forks a fresh worker
**Then** `build_tool_registry` discovers it (importing each live tool module, the plugin-host `pkgutil`/`importlib` convention) and registers it as a FREE-tier tool, callable in the loop — no process restart (the fork-reimport property)
**And** a live tool module that fails to import or is malformed is skipped + logged (the turn survives, AD-8 quarantine discipline) — a self-coded tool never wedges the worker

### AC5 — Spine invariants + boundary gate

**Given** 9.4 lands
**Then** `core/` imports no LLM/provider code (import-linter 3 contracts KEPT); core is the sole writer of the workspace tool dirs (AD-5); a bad/oversized proposal fails soft (never crashes a turn); the dynamic FREE discovery does not change the broker seam or the agentic loop
**And** all existing tests pass (630+), import-linter 3 contracts green, `uv sync --locked` 0 new deps (`pytest` is already a dev dep; `subprocess`/`importlib`/`ast`/`shutil` stdlib)

---

## Tasks / Subtasks

- [x] **Task 1 — Contract: `ProposeTool` op** (AC1)
  - [x] `shelldon/contracts/__init__.py`: add `ProposeTool(msgspec.Struct, frozen, tag="propose_tool", forbid_unknown_fields)` with `name: str`, `code: str`, `test: str`. Add to the `ProposedOp` union (next to `RequestToolApproval`) and `__all__`.
  - [x] No SCHEMA_VERSION bump (additive op on the existing union — AD-13). Verify round-trip.
  - [x] It rides `parse_reply` → `proposed_ops` automatically (the `_OPS_DECODER` decodes the whole `ProposedOp` union) — NO worker-loop change.

- [x] **Task 2 — `core/selfcode.py`: stage / gate / promote / discard + dir layout** (AC1, AC2, AC3)
  - [x] Relocate `DEFAULT_WORKSPACE_ROOT` here from `worker/tools.py` (so core owns the workspace layout without a core→worker import). `worker/tools.py` and `app.py` import it from `core.selfcode` instead (worker→core / app→core are allowed; core→worker is the smell we avoid). Add `live_tools_dir(ws)` = `ws/"tools"`, `staging_dir(ws)` = `ws/"tools-staging"`.
  - [x] `_safe_tool_name(name) -> str` — slugify to a safe module stem (reuse `core/memory._safe_filename`'s discipline: no separators/`..`/dots); reject empty.
  - [x] `stage(name, code, test, *, workspace_root) -> (module_path, test_path)` — write `<stem>.py` (code) + `test_<stem>.py` (test) to the staging dir (created if absent). Cap code/test size (reuse a sane cap).
  - [x] `async run_gate(stem, *, workspace_root, timeout_s) -> (passed: bool, output: str)` — (1) AST import-check the staged module: reject if it imports any of the import-linter LLM set (`openai`/`anthropic`/`google`/`litellm`/`zhipuai`/`ollama`) or `shelldon.core`; (2) `asyncio.create_subprocess_exec(<python>, "-m", "pytest", "-q", test_path, cwd=staging)` bounded by `timeout_s` (kill on timeout = fail). Return capped combined output.
  - [x] `promote(stem, *, workspace_root)` — move `<stem>.py` (+ drop the test, or move it too) from staging to the live dir; `discard(stem, ...)` — delete the staged pair. Both fail-soft.

- [x] **Task 3 — sqlite: pending_promotions (parallel to 9.3's pending_approvals)** (AC2, AC3)
  - [x] `shelldon/core/history.py`: add a `pending_promotions` table — `(turn_id TEXT PRIMARY KEY, created_at TEXT, expires_at TEXT, tool_name TEXT)` (additive CREATE-IF-NOT-EXISTS; no blob — the staged files are on disk).
  - [x] `park_promotion(turn_id, tool_name, now, ttl)` + `take_promotion(turn_id, now) -> str | None` (atomic read+expiry+delete, mirroring `take_approval`; expired/absent → None) + `prune_expired_promotions(now)`.
  - [x] Kept SEPARATE from `pending_approvals` so Story 9.3's resume table/signatures are UNTOUCHED (a tap dispatches by which table holds the turn id — see Task 4).

- [x] **Task 4 — core/runtime: handle ProposeTool + dispatch the approval tap** (AC1, AC2, AC3)
  - [x] `_handle_result`: after the reply, if the result carries a `ProposeTool` op, `await self._handle_propose_tool(op)` (async — the gate is a subprocess). Do NOT put ProposeTool in the SYNC `_apply_proposed_ops` (it needs `await`).
  - [x] `_handle_propose_tool(op)`: `selfcode.stage(...)` → `await selfcode.run_gate(...)`; on FAIL → `discard` + `_send_reply("tool `name` failed: <reason>")`; on PASS → `history.park_promotion(turn_id, name, now)` + `_send_reply("…passed its test — add it?", approval_turn_id=turn_id)` (the 9.3 keyboard).
  - [x] `_handle_approval_decision(turn_id, approved)`: FIRST try `history.take_promotion(turn_id, now)` — if a tool_name comes back, it's a 9.4 promotion (a quick file op + reply, no worker/slot needed): `selfcode.promote` (approve) or `selfcode.discard` (deny) + confirm. If None, fall through to the EXISTING 9.3 resume path (take_approval → spawn_resume). Order matters: promotion needs no ≤1 slot, so check it before the `arbiter.is_idle` guard.
  - [x] Core constructs a `SelfCoder`/uses `core.selfcode` with its configured `workspace_root` (add a `workspace_root` to `Core.__init__`, defaulting to `core.selfcode.DEFAULT_WORKSPACE_ROOT`).

- [x] **Task 5 — worker/tools.py: discover live self-coded tools (FREE)** (AC4)
  - [x] `discover_self_coded_tools(workspace_root) -> list[ToolSpec]` — iterate `*.py` in `live_tools_dir(workspace_root)`; import each via `importlib.util.spec_from_file_location` (NOT on sys.path); build a `ToolSpec(name=stem, description=mod.DESCRIPTION, params_schema=mod.PARAMS_SCHEMA, tier=ToolTier.FREE, fn=mod.run)`. A module missing the convention or raising on import → skip + log (AD-8; quarantine is 9.5).
  - [x] Tool-module CONVENTION (document in the SYSTEM_INSTRUCTION too): a tool module defines `run(**kwargs) -> str`, `DESCRIPTION: str`, `PARAMS_SCHEMA: dict` at module level — NO shelldon imports needed (keeps it import-clean + simple for the model to emit).
  - [x] `build_tool_registry(...)`: after the built-in specs, merge in `discover_self_coded_tools(ws)` (a discovered tool may NOT shadow a built-in name — built-ins win, log a skip).

- [x] **Task 6 — worker/prompt.py: tell the model how to propose a tool** (AC1)
  - [x] Append a short `SYSTEM_INSTRUCTION` clause: the model MAY write a new FREE tool by emitting a `propose_tool` op (`{"type":"propose_tool","name":"...","code":"...","test":"..."}`) — the code must define `run`/`DESCRIPTION`/`PARAMS_SCHEMA`, ship with a pytest test, import no LLM libs, and it goes live only after the owner approves. Match the existing ops-block tone.

- [x] **Task 7 — app.py: ensure the live + staging dirs exist** (AC4)
  - [x] After the 9.2 `os.makedirs(DEFAULT_WORKSPACE_ROOT)`, also create `live_tools_dir` + `staging_dir` (normal perms; the worker reads/imports the live dir). Import the helpers from `core.selfcode`.

- [x] **Task 8 — Tests** (AC1–AC5)
  - [x] `tests/test_selfcode.py` (NEW): `stage` writes the pair; `run_gate` passes a good tool+test, fails a tool whose test fails, rejects a tool importing `anthropic`/`shelldon.core` (import-check), times out a hanging test; `promote` moves staged→live; `discard` deletes.
  - [x] `tests/test_self_coded_discovery.py` (NEW): a live tool module (run/DESCRIPTION/PARAMS_SCHEMA) is discovered + registered FREE + callable via `execute_tool`; a malformed module is skipped (turn survives); a discovered tool can't shadow a built-in.
  - [x] `tests/test_selfcode_flow.py` (NEW, core-level fake spawner like test_risky_approval): a `ProposeTool` op → staged + gated; PASS → pending_promotion parked + approval reply tagged; Approve tap → promoted (live file exists); Deny → discarded; FAIL gate → reply + nothing parked; expired promotion dropped.
  - [x] `tests/test_history.py` (UPDATE): park/take/expire pending_promotions.
  - [x] Contracts round-trip for `ProposeTool`.
  - [x] Boundary gate: `uv run pytest -q` → all pass (630+ baseline + new); `uv run lint-imports` → 3 KEPT; `uv sync --locked` → 0 new deps.

---

## Dev Notes

### What 9.1–9.3 already built (read first)

- **`worker/tools.py`** — `ToolSpec(name, description, params_schema, tier, fn)`, `execute_tool` (schema-filters args, catches all → `ToolResult(ok=False)`), `build_tool_registry(workspace_root, memory_root)`, `DEFAULT_WORKSPACE_ROOT` (RELOCATING to `core.selfcode` in Task 2), the FREE pack (9.2) + RISKY pack (9.3). 9.4 adds dynamic FREE discovery to `build_tool_registry`.
- **`contracts/__init__.py`** — `ProposedOp` union (now incl. `RequestToolApproval`); `parse_reply` (worker) decodes ANY `ProposedOp` from a ```ops block. `ProposeTool` joins the union and rides this wire for free.
- **`core/runtime.py`** — `_handle_result` (reply → `_apply_proposed_ops` (SYNC) → record → release); `_handle_approval_decision` + `_start_resume_turn` (9.3); `_send_reply(text, *, approval_turn_id=None)` already renders the 9.3 keyboard. 9.4 adds an async `_handle_propose_tool` + a promotion branch in `_handle_approval_decision`.
- **`core/history.py`** — `pending_approvals` table + `park_approval`/`take_approval`/`prune` (9.3). 9.4 adds a PARALLEL `pending_promotions` table (don't touch 9.3's).
- **`plugins/host.py::discover_plugins`** (read it) — the `pkgutil.iter_modules` + `importlib.import_module` + per-module try/except skip pattern (AD-8). 9.4's discovery mirrors it but over a workspace DIR (use `importlib.util.spec_from_file_location` since the dir isn't a package on `sys.path`).
- **9.3 approval surface** — `OutboundMessage.approval_turn_id` (keyboard) + `InboundMessage.approval_turn_id/approved` (tap) + the Telegram render. 9.4 REUSES it verbatim; the only new thing is core dispatching a tap to promotion vs resume.

### Architecture constraints

- **Core stays LLM-free (import-linter).** `core/selfcode.py` runs `pytest` via `asyncio.create_subprocess_exec` — a subprocess, NOT an import; no LLM lib enters core. The import-check on staged code is an `ast` scan (stdlib).
- **Core is the sole writer of the workspace tool dirs (AD-5).** Staging/promote/discard are core file ops. The worker only READS/imports the live dir (discovery).
- **Avoid core→worker imports.** Relocate `DEFAULT_WORKSPACE_ROOT` + the dir helpers to `core.selfcode`; `worker/tools.py` imports them (worker→core, already the pattern via `core.memory`). Discovery (which builds `ToolSpec`, a worker type) stays in `worker/tools.py`.
- **Fail-soft + quarantine (AD-8).** A bad proposal → gate fail → discard + note. A bad LIVE module → skip + log at discovery (the turn survives). Repeated-bad-tool quarantine + resource caps are Story 9.5.
- **Fork = no accumulation (AD-3).** Discovery runs in each fresh fork's `build_tool_registry`; the live dir is re-imported per turn (the fork-reimport property — the whole reason this is cheap in Python).

### The gate runs untrusted code (accepted, single-owner)

Running `pytest` on the model's test EXECUTES model-written code BEFORE the owner approves (the test imports + calls the tool). This is inherent to "run the test to verify it." Accepted for single-owner (design §6): bound it with a subprocess timeout + run from the staging cwd; the owner still reviews the code at the Approve step. Deeper sandboxing (RLIMIT, no-network) is Story 9.5.

### Self-coded tools are FREE once promoted (locked decision 1)

The owner reviewing the code at the Approve tap IS the gate. Promoted tools register FREE (`ToolTier.FREE`) → they run inline every turn with no further tap. This is the v1 magic. (A tool that itself needs risky ops would call the existing RISKY tools, which still gate — so a FREE self-coded tool can't silently escalate.)

### No new dependencies

`pytest` is already a dev dependency (the suite runs on it). `subprocess`/`asyncio`/`importlib`/`ast`/`shutil`/`pathlib` are stdlib. `uv sync --locked` must show 0 changes. (Note: the gate invokes `pytest` — ensure it's available in the runtime env; on the Pi the venv has it. If prod images strip dev deps, that's a deploy note for 9.5, not a new dep here.)

### What 9.5 adds next (do NOT pull in)

Quarantine of repeatedly-broken live tools (move to `tools-quarantine/`), RLIMIT CPU/mem caps on the gate + `run_shell`/`python_eval`, and credit-tier gating. 9.4 ships skip-on-bad-import + a bounded gate timeout only.

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Dev Story workflow)

### Debug Log References

- `uv run pytest -q` → 660 passed, 3 skipped (pre-existing FTS5-absent simulations), 7 deselected (`-m live`). Baseline was 633 collected; +27 new tests.
- `uv run lint-imports` → 3 contracts KEPT (core LLM-free / transport creds / plugins-never-import-core).
- `uv sync --locked` → 0 changes (33 packages audited). `pytest` already a dev dep; `subprocess`/`asyncio`/`importlib`/`ast`/`shutil`/`unicodedata` are stdlib.

### Completion Notes List

- **Gate placement (design call).** `_handle_propose_tool` is `await`ed inline in `_handle_result` (after reply + ops + record, before the slot release), NOT backgrounded. Rationale: single-owner; the gate is a bounded async subprocess (`asyncio.create_subprocess_exec` of `pytest`, killed on timeout = fail), so the event loop keeps servicing timers/reaps during the await. The worker fork has already exited (its Result arrived), so no fork is held during gating. Tradeoff: the ≤1 arbiter slot stays reserved for the gate's duration (bounded by `DEFAULT_GATE_TIMEOUT_S`), briefly coalescing any concurrent owner message — acceptable for a single-owner pet and keeps the flow deterministic + simply testable. Matches Task 4's literal `await self._handle_propose_tool(op)`.
- **`DEFAULT_WORKSPACE_ROOT` relocated** to `core/selfcode.py`; `worker/tools.py` re-exports it (so `worker.tools.DEFAULT_WORKSPACE_ROOT` still resolves for any existing caller) and `app.py` now imports it (+ `live_tools_dir`/`staging_dir`) from `core.selfcode`. No core→worker import introduced.
- **`ProposeTool` skipped in the SYNC `_apply_proposed_ops`** (an explicit `continue`) so it never routes to `apply_memory_op` (which would reject it) — it is handled on the async path only.
- **Promotion dispatch precedes the `arbiter.is_idle` guard** in `_handle_approval_decision`: a tool promotion is a file move + reply (no ≤1 slot), so it is checked/consumed before the 9.3 resume admission. An expired/unknown promotion `take_promotion` → None falls through cleanly to the 9.3 path (→ "expired or no longer pending"), so AC3's expiry discipline holds with no new branch.
- **Promote drops the staged test** (moves only `<stem>.py` to live) so discovery — which imports every `*.py` — never tries to import a `test_*.py`. Discovery also skips `_`/`test_`-prefixed files defensively.
- **Conftest isolation:** added `_app.DEFAULT_WORKSPACE_ROOT` → `tmp_path/workspace` to the autouse fixture, so the app-smoke turn creates the new live/staging dirs under tmp, never real `~/.shelldon` (the pre-existing unconditional `os.makedirs` already created the workspace; this keeps the new subdirs off `$HOME` too).
- **AC5 invariants verified:** import-linter 3 KEPT (core stays LLM-free — the gate is a subprocess + an `ast` scan, no SDK import); core is the sole writer of the tool dirs (stage/promote/discard are core file ops; the worker only reads/imports the live dir); a bad/oversized proposal fails soft (stage raises → caught → owner note; gate fail/timeout → discard + note; a malformed LIVE module is skipped + logged at discovery, AD-8). The broker seam + agentic loop are unchanged — only `build_tool_registry` gained the discovery merge.

### File List

- shelldon/contracts/__init__.py (UPDATE — ProposeTool op + ProposedOp union + __all__)
- shelldon/core/selfcode.py (NEW — DEFAULT_WORKSPACE_ROOT + dir helpers, stage/run_gate/promote/discard)
- shelldon/core/history.py (UPDATE — pending_promotions table + park/take/prune)
- shelldon/core/runtime.py (UPDATE — _handle_propose_tool, promotion dispatch in _handle_approval_decision, workspace_root)
- shelldon/worker/tools.py (UPDATE — import DEFAULT_WORKSPACE_ROOT from core.selfcode; discover_self_coded_tools; merge into build_tool_registry)
- shelldon/worker/prompt.py (UPDATE — propose_tool instruction)
- shelldon/app.py (UPDATE — create live + staging dirs; import path moved to core.selfcode)
- tests/test_selfcode.py (NEW)
- tests/test_self_coded_discovery.py (NEW)
- tests/test_selfcode_flow.py (NEW)
- tests/test_history.py (UPDATE — pending_promotions park/take/expire/prune + independence)
- tests/test_contracts_roundtrip.py (UPDATE — ProposeTool)
- tests/conftest.py (UPDATE — redirect app's DEFAULT_WORKSPACE_ROOT to tmp_path)

### Change Log

- 2026-06-22 — Review findings addressed: 3 [Patch] items resolved (`from shelldon import core` import-check gap, `run_gate` timeout `ProcessLookupError` guard, failed-promote orphan cleanup); 8 [Defer] items confirmed → 9.5 hardening scope. +3 tests (663 pass).
- 2026-06-22 — Story 9.4 implemented (persistent self-coded tools). `ProposeTool` op joins the closed `ProposedOp` union (no SCHEMA_VERSION bump); new `core/selfcode.py` owns the workspace tool dirs + stage/gate/promote/discard (gate = AST import-check + bounded `pytest` subprocess, core stays LLM-free); `pending_promotions` sqlite table parallels 9.3's `pending_approvals`; core `_handle_propose_tool` + promotion-first dispatch in `_handle_approval_decision` ride the 9.3 Approve/Deny keyboard; the worker discovers promoted tools FREE via `discover_self_coded_tools` merged into `build_tool_registry` (built-ins win shadows; bad modules skipped, AD-8). 27 new tests (660 pass), import-linter 3 KEPT, 0 new deps. Status → review.

### Review Findings

- [x] [Review][Patch] `_forbidden_import` misses `from shelldon import core` — `mod="shelldon"` doesn't match `"shelldon.core"` check; tool can reach back into core [selfcode.py:`_forbidden_import`] — FIXED: also build each alias's fqn (`{mod}.{alias.name}`) and reject `shelldon.core[.*]`. +7-case parametrized test (`test_forbidden_import_catches_all_reach_into_core_or_llm`) + clean-module negatives.
- [x] [Review][Patch] `run_gate` timeout handler: `proc.kill()` raises `ProcessLookupError` if process already dead — breaks "Never raises" contract [selfcode.py:`run_gate`] — FIXED: `proc.kill()` wrapped in `try/except ProcessLookupError`.
- [x] [Review][Patch] Failed `promote()` never calls `discard()` — staged files left on disk permanently when `shutil.move` fails [runtime.py:`_handle_approval_decision`] — FIXED: a False promote now discards the staged pair before the "didn't promote" reply. New test `test_failed_promote_discards_staged_files` (monkeypatches promote→False).
- [x] [Review][Defer] Dynamic imports (`__import__`, `importlib.import_module`) bypass AST check [selfcode.py:`_forbidden_import`] — deferred → 9.5 sandboxing scope
- [x] [Review][Defer] Python keyword as tool name (`class`, `def`) — no `keyword.iskeyword()` guard in `_safe_tool_name` [selfcode.py:`_safe_tool_name`] — deferred → unlikely, 9.5 scope
- [x] [Review][Defer] Slugification collision: `foo-bar` and `foo_bar` → same stem, second stage silently overwrites first [selfcode.py:`stage`] — deferred → unlikely, 9.5 scope
- [x] [Review][Defer] `asyncio.CancelledError` during gate leaves subprocess orphaned — bypasses `except Exception` cleanup [selfcode.py:`run_gate`] — deferred → 9.5 resource cleanup scope
- [x] [Review][Defer] Only first `ProposeTool` per turn handled via `next()`, rest silently dropped [runtime.py:`_handle_result`] — deferred → single-tool-per-turn by design
- [x] [Review][Defer] `prune_expired_promotions` has no call site — consistent with 9.3's `prune_expired_approvals` pattern [history.py] — deferred → schedule with 9.5 hardening pass
- [x] [Review][Defer] Stale `test_<stem>.py` orphan in staging after a rename/overwrite [selfcode.py:`stage`] — deferred → cosmetic, staging not scanned for live tools
- [x] [Review][Defer] `ProposeTool` in `proposed_ops` bypasses `MAX_PROPOSED_OPS` cap if at position 17+ [runtime.py:`_handle_result`] — deferred → very unlikely in practice
- [x] [Review][Defer] Silent overwrite of previously promoted live tool on re-proposal [selfcode.py:`promote`] — deferred → by design intent (model updating its own tool)
