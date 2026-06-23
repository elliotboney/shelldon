---
baseline_commit: 03b93b779edb45342d769edde973aa14bd3e76f2
---

# Story 9.5: Safety hardening

Status: done

## Story

As the owner,
I want broken or runaway tools to never wedge or bankrupt the pet,
so that live self-coding is safe to leave running on the 416MB Pi.

## Scope decisions (locked with Elliot 2026-06-22)

1. **Pillars + cheap cleanup** — 9.5 ships the epic's three safety pillars (quarantine, RLIMIT resource caps, credit/loop gating) PLUS the low-cost, high-value defers that ride along (prune scheduling, gate `CancelledError` cleanup, `keyword`/slug guards, dynamic-import detection, the `ProposeTool` op-cap fix, and a few audit logs).
2. **Network/shell POLICY items → a future 9.6** — explicitly OUT OF SCOPE here: `http_get` SSRF-on-redirect blocking, `http_get` streaming/pre-read byte cap, `git` subcommand allowlist, `run_shell` process-group cleanup, and the `_deny_sensitive` credential-blocklist expansion. Each is its own policy surface and already sits behind the 9.3 owner-approval gate. They remain tracked in `deferred-work.md` (9.2/9.3 review sections) and should become Story **9.6 (tool-policy hardening)**.
3. **RLIMIT enforcement target is Linux/the Pi** — the per-turn worker is a Linux-only fork in practice (macOS aborts fork-without-exec), so the worker-fork rlimits are naturally Linux-gated. Tests assert the limits are SET (inspect/inject), not the OOM/SIGXCPU kill behavior (which is cross-platform-flaky).
4. **Credit gating reuses Story 5.2 verbatim** — no new $/token accounting. The worker loop ceiling (9.1 `_MAX_TOOL_EXECUTIONS`) is the hard per-turn model-call cap; scheduler-initiated tool turns carry a 5.2 `cost` weight so the daily turn budget already bounds total self-driven spend. Owner turns stay un-budgeted (5.2 design) but remain loop-ceiling-bounded.

## Acceptance Criteria

### AC1 — Quarantine: a repeatedly-bad live tool is moved out of the way (epic AC1)

**Given** a promoted self-coded tool in the live dir that errors on import OR raises when called
**When** the next fresh worker loads/calls it
**Then** it is skipped + logged and the turn survives (already shipped in 9.4 discovery + `execute_tool` fail-soft — AD-8) — re-verified here
**And** the worker reports the failing self-coded tool name(s) on its `Result` (additive optional field, no `SCHEMA_VERSION` bump — AD-13), core debits a per-tool strike count in a new core-owned sqlite ledger, and when a tool crosses the strike threshold (default 3) **core** (sole writer of the tool dirs, AD-5) MOVES it from `tools/` to `tools-quarantine/` — so the next fork's `build_tool_registry` no longer discovers it (the faces-registry "single-writer + move" pattern), logged
**And** a quarantined tool never wedges the worker and never re-enters discovery until the owner restores it manually (auto-rehabilitation is out of scope)

### AC2 — Resource caps: the 416MB Pi cannot be OOM'd or CPU-pegged by a tool (epic AC2, part 1)

**Given** the per-turn worker fork (AD-3) and the subprocesses it/core spawn (`run_shell`, the 9.4 gate `pytest`)
**When** a worker turn runs
**Then** the fork child sets `RLIMIT_AS` (address space) + `RLIMIT_CPU` on itself before running the turn, bounding `python_eval`, any FREE self-coded tool, and the loop as a whole — a breach raises `MemoryError`/`SIGXCPU` that is caught fail-soft (`ToolResult(ok=False)` / the turn degrades), never an OOM of the Pi
**And** the `run_shell` subprocess (9.3) and the `run_gate` `pytest` subprocess (9.4) set `RLIMIT_CPU`/`RLIMIT_AS` via a `preexec_fn` so a spawned child can't escape the worker's cap either
**And** the caps are injectable/config-defaulted (a sane default sized for the 416MB Pi), and `python_eval`'s existing SIGALRM wall-bound (9.2) is kept — RLIMIT is the C-level/memory backstop SIGALRM can't provide (closes the 9.2 defer)

### AC3 — Credit / loop gating: a runaway loop can't burn the budget (epic AC2, part 2)

**Given** the bounded agentic loop (9.1) which may make multiple model calls per turn
**When** it runs
**Then** the per-turn model-call ceiling (`_MAX_TOOL_EXECUTIONS`) is affirmed as the hard runaway backstop with a conservative default and a logged note on exhaustion (already present — 9.5 verifies + documents the bound)
**And** any SCHEDULER-initiated turn that can run the tool-loop carries a Story 5.2 `cost` weight ≥ its worst-case loop multiplier, so the daily turn budget bounds total self-driven spend (worst case = `daily_turn_budget` × loop-ceiling model calls); the bounded-spend invariant is documented. NO new wire/credit field — pure 5.2 reuse
**And** owner-initiated turns remain un-budgeted (5.2 design) but loop-ceiling-bounded

### AC4 — Cheap cleanup that rides along (the low-cost 9.2/9.4 review defers)

**Given** the 9.1–9.4 surfaces
**Then** all of the following land (each small, each tested):
- **Prune scheduling** — a REFLEX-tier scheduler job periodically calls `history.prune_expired_approvals(now)` + `history.prune_expired_promotions(now)` (the existing methods have no call site); modeled on the existing `checkpoint`/`reflex` jobs
- **Gate `CancelledError` cleanup** — `selfcode.run_gate` kills + awaits its `pytest` subprocess on `asyncio.CancelledError` (a `BaseException`, currently bypassing `except Exception`) via `try/finally`, so teardown never orphans a gate subprocess
- **`keyword` tool-name guard** — `selfcode._safe_tool_name` rejects/suffixes a Python keyword/soft-keyword stem (`class`, `def`, `match`) so the live module is always importable
- **Slug-collision + stale-test hygiene** — `selfcode.stage` discards any prior staged pair for the stem before writing (no orphan `test_<stem>.py`) and logs a warning when it overwrites an existing staged file
- **Dynamic-import detection** — `selfcode._forbidden_import` also flags `__import__("…")` / `importlib.import_module("…")` calls whose literal arg is in the forbidden set (defense-in-depth on the gate; a non-literal/dynamic arg is logged as unverifiable)
- **`ProposeTool` op-cap consistency** — `_handle_result` searches for the `ProposeTool` op WITHIN the `MAX_PROPOSED_OPS`-capped slice (not the full list), and logs a warning if a turn carries more than one `ProposeTool` (only the first is handled — by design)
- **Promote audit log** — `selfcode.promote` logs an info line when it overwrites an existing live tool (the model updating its own tool — by design, but auditable)

### AC5 — Spine invariants + boundary gate

**Given** 9.5 lands
**Then** `core/` imports no LLM/provider code (import-linter 3 contracts KEPT — `resource`/`shutil`/`sqlite3`/`keyword`/`ast` are stdlib); core remains the sole writer of the workspace tool dirs incl. `tools-quarantine/` (AD-5); every new guard fails soft (a bad/oversized/runaway tool never crashes a turn); the broker seam and the agentic loop shape are unchanged
**And** the OUT-OF-SCOPE 9.6 items (SSRF redirect-block, `http_get` streaming cap, `git` allowlist, `run_shell` process-group, `_deny_sensitive` credential-blocklist) are NOT pulled in — they stay in `deferred-work.md`
**And** all existing tests pass (672+ baseline), import-linter 3 contracts green, `uv sync --locked` 0 new deps

---

## Tasks / Subtasks

- [x] **Task 1 — Quarantine: core-owned strike ledger + move-to-quarantine** (AC1)
  - [x] `shelldon/core/selfcode.py`: add `quarantine_dir(ws)` = `ws/"tools-quarantine"` and `quarantine(stem, *, workspace_root) -> bool` — move `<stem>.py` from `tools/` to `tools-quarantine/` (fail-soft, mkdir the dir; mirrors `promote`).
  - [x] `shelldon/core/history.py`: add a `tool_health` table — `(name TEXT PRIMARY KEY, strikes INTEGER NOT NULL DEFAULT 0, last_seen TEXT NOT NULL)` (additive CREATE-IF-NOT-EXISTS). `record_tool_failure(name, now) -> int` (atomic UPSERT increment, returns the new strike count) + `clear_tool_health(name)` (reset on a successful call — optional, keep simple) + a reader for tests.
  - [x] `shelldon/worker/tools.py`: `discover_self_coded_tools` collects the names it skips (import/convention failure) and surfaces them; `execute_tool` / the loop tracks self-coded tools that return `ok=False`. A `ToolSpec` may need a `self_coded: bool = False` flag (worker-only dataclass field) so the loop can tell a self-coded run-failure from a built-in one.
  - [x] `shelldon/contracts/__init__.py`: add `tool_failures: tuple[str, ...] = ()` to `Result` (additive optional default — no `SCHEMA_VERSION` bump, AD-13). Round-trip test.
  - [x] `shelldon/worker/worker.py`: `run_worker` collects the turn's failing self-coded tool names (discovery skips + run-failures) and sets `Result.tool_failures`.
  - [x] `shelldon/core/runtime.py`: in `_handle_result`, after the reply, for each name in `result.tool_failures` call `history.record_tool_failure`; at the threshold (default 3, a `Core` const) call `selfcode.quarantine` + log. Fail-soft (guarded like the ops apply).

- [x] **Task 2 — RLIMIT resource caps** (AC2)
  - [x] `shelldon/worker/forkserver.py`: in the fork child (`_os_fork_spawn`), AFTER the privilege drop and BEFORE `asyncio.run(run_worker(...))`, set `resource.setrlimit(RLIMIT_AS, …)` + `RLIMIT_CPU` from injectable caps (defaults sized for the 416MB Pi — e.g. AS ≈ 256–300MB, CPU ≈ the 25s loop ceiling + slack). Linux-gated by the real-fork path; guard with try/except so a platform without a given RLIMIT logs + continues.
  - [x] `shelldon/worker/tools.py`: `_run_subprocess` (the `run_shell`/`git` runner) passes a `preexec_fn` that sets `RLIMIT_CPU`/`RLIMIT_AS` on the child. Keep the existing `_RISKY_TIMEOUT_S` wall-bound.
  - [x] `shelldon/core/selfcode.py`: `run_gate`'s `create_subprocess_exec` passes the same `preexec_fn` (a small shared helper; core may import `resource` — stdlib, import-linter unaffected).
  - [x] Keep `python_eval`'s SIGALRM wall-bound (9.2); RLIMIT_AS on the worker fork is the memory/C-level backstop. Document the layering.

- [x] **Task 3 — Credit / loop gating** (AC3)
  - [x] Affirm `_MAX_TOOL_EXECUTIONS` (worker) as the hard per-turn model-call cap; confirm the conservative default + the exhaustion log (already present). Add a docstring/comment stating the bounded-spend invariant (daily_budget × loop_ceiling).
  - [x] Ensure any tool-capable SCHEDULER turn job (e.g. the dream, `cost=3` today) carries a `cost` weight covering its worst-case loop multiplier — adjust the registered `Job(cost=…)` if needed, with a comment tying it to the loop ceiling. NO new wire field; pure 5.2 reuse.
  - [x] Test: a scheduler tool-turn debits the daily budget by its `cost` (5.2 path already does this — assert the bound holds with a tool-loop turn).

- [x] **Task 4 — Cheap cleanup** (AC4)
  - [x] `core/runtime.py`: register a REFLEX-tier `prune` scheduler job (interval like `checkpoint`) that calls `history.prune_expired_approvals(now)` + `history.prune_expired_promotions(now)`. Guarded by the scheduler like the other reflex jobs.
  - [x] `core/selfcode.py::run_gate`: wrap the subprocess in `try/finally` that kills + awaits it on `CancelledError` (and any `BaseException`), so teardown never orphans the gate.
  - [x] `core/selfcode.py::_safe_tool_name`: `import keyword`; if the stem `keyword.iskeyword()`/`issoftkeyword()`, suffix `_tool` (keep it usable, don't reject) — the module must stay importable.
  - [x] `core/selfcode.py::stage`: `discard(stem, …)` the prior pair before writing (no stale `test_<stem>.py`); log a warning if it overwrites an existing staged module.
  - [x] `core/selfcode.py::_forbidden_import`: walk `ast.Call` for `__import__`/`importlib.import_module` with a string-literal arg in the forbidden set → reject; a non-literal arg → log "unverifiable dynamic import" (don't hard-reject — owner-approval is the backstop).
  - [x] `core/runtime.py::_handle_result`: search for the `ProposeTool` op within the `MAX_PROPOSED_OPS`-capped slice; warn if >1 `ProposeTool` in a turn (only the first is handled).
  - [x] `core/selfcode.py::promote`: log an info line when overwriting an existing live tool of the same name.

- [x] **Task 5 — Tests** (AC1–AC5)
  - [x] `tests/test_selfcode.py` (UPDATE): `quarantine` moves live→quarantine; `run_gate` `CancelledError` kills the subprocess (no orphan); `_safe_tool_name` keyword guard; `stage` clears a stale test + warns on overwrite; `_forbidden_import` catches `__import__("anthropic")`/`importlib.import_module("openai")`.
  - [x] `tests/test_self_coded_discovery.py` (UPDATE): discovery surfaces skipped self-coded names; a self-coded run-failure is attributable.
  - [x] `tests/test_selfcode_flow.py` (UPDATE): a self-coded tool failing `N` times is quarantined by core (live file moved, no longer discovered); under the threshold it stays live.
  - [x] `tests/test_history.py` (UPDATE): `tool_health` record/increment/threshold + (if added) clear.
  - [x] `tests/test_resource_caps.py` (NEW): the fork child sets `RLIMIT_AS`/`RLIMIT_CPU` (inject a recording `setrlimit`/inspect the preexec_fn); `_run_subprocess` + `run_gate` pass a `preexec_fn` that sets the rlimits. Assert limits are SET, not kill behavior (cross-platform).
  - [x] `tests/test_contracts_roundtrip.py` (UPDATE): `Result.tool_failures` round-trips + plain `Result` defaults to `()`.
  - [x] Prune-job test: the scheduler `prune` job calls both prune methods (fake/inspect).
  - [x] Boundary gate: `uv run pytest -q` → all pass (672+ baseline + new); `uv run lint-imports` → 3 KEPT; `uv sync --locked` → 0 new deps.

---

## Dev Notes

### What 9.1–9.4 already built (read first)

- **`shelldon/core/selfcode.py`** (9.4) — owns the workspace tool dirs (`DEFAULT_WORKSPACE_ROOT`, `live_tools_dir`, `staging_dir`) + `stage`/`run_gate`/`promote`/`discard`. `run_gate` = an `ast` import-check (`_forbidden_import`) + a bounded `asyncio.create_subprocess_exec` of `pytest`. 9.5 adds `quarantine_dir`/`quarantine`, the `CancelledError` cleanup, the keyword/slug/dynamic-import guards, and the `preexec_fn` rlimit on the gate subprocess. **Core stays LLM-free** — `resource` is stdlib.
- **`shelldon/worker/tools.py`** (9.1–9.4) — `ToolSpec(name, description, params_schema, tier, fn)`, `execute_tool` (fail-soft, schema-filters args), `build_tool_registry` (merges `discover_self_coded_tools` FREE; built-ins win shadows), `_run_subprocess` (the RISKY `run_shell`/`git` runner, `_RISKY_TIMEOUT_S` wall-bound). 9.5 adds: a `self_coded` flag on `ToolSpec`, discovery surfacing skipped names, and the `preexec_fn` rlimit on `_run_subprocess`.
- **`shelldon/worker/forkserver.py`** — `_os_fork_spawn` is the per-turn fork (AD-3): child does `_close_inherited_sqlite()` → `os.closerange` → `_maybe_drop_privileges` → `asyncio.run(run_worker(...))`. 9.5 sets the worker rlimits in this child right before `asyncio.run`. The fork is Linux-only in practice (macOS aborts fork-without-exec), so the worker-fork rlimits are naturally Linux-gated.
- **`shelldon/core/runtime.py`** — `_handle_result` (reply → ops → record → `_handle_propose_tool` (9.4) → release), the scheduler (`reflex`/`checkpoint`/`proactive`/`dream` jobs registered in `__init__`), `_apply_proposed_ops` (`MAX_PROPOSED_OPS=16` cap; ProposeTool skipped here, handled async in `_handle_result`). 9.5 adds the quarantine debit, the `prune` scheduler job, and the ProposeTool op-cap fix.
- **`shelldon/core/history.py`** — sqlite WAL store; `pending_approvals` (9.3) + `pending_promotions` (9.4) each with `park`/`take`/`prune_expired_*` — **the `prune_expired_*` methods exist but have NO call site** (9.5 schedules them). The `learnings` table's atomic-UPSERT pattern is the model for `tool_health`'s strike increment.
- **`shelldon/core/budget.py` + `core/dispatch.py`** (5.2) — `BudgetGate` is the daily turn-COUNT budget + cooldown for SCHEDULER turns; `TurnDispatcher.dispatch_turn_job` debits `job.cost` at admission (`admission_patch`). `cost` is a turn-count WEIGHT (dream = `cost=3`), NOT dollars. This is the credit-gating seam to reuse verbatim — no new field.

### The three pillars (recommended designs)

- **Quarantine = worker DETECTS, core DECIDES + MOVES (AD-5).** The worker is ephemeral (forks die) so it can't accumulate a cross-turn failure count; the count lives in a core-owned sqlite table (`tool_health`). The worker reports failing self-coded tool names on `Result.tool_failures`; core strikes them and, at the threshold, moves the live module to `tools-quarantine/` (core is the sole writer of the tool dirs). This is the faces-registry "single-writer + atomic move" pattern over a workspace dir. Import-failures and run-failures both count.
- **Resource caps = RLIMIT on the per-turn fork + preexec_fn on spawned children.** `RLIMIT_AS` on the worker fork bounds the ENTIRE worker (python_eval, FREE self-coded tools, the loop) — a runaway allocation raises `MemoryError` (caught fail-soft) instead of OOMing the 416MB Pi; the fork dies each turn anyway (AD-3), so the cap is per-turn-clean. `RLIMIT_CPU` bounds CPU. `run_shell`/`git`/the gate `pytest` run in their own child processes → a `preexec_fn` sets the same rlimits so they can't escape. Keep `python_eval`'s SIGALRM wall-bound (9.2) — RLIMIT is the C-level/memory backstop SIGALRM (Python-bytecode-boundary only) can't give. Linux is the enforcement target; tests assert the limits are SET.
- **Credit gating = loop ceiling × 5.2 budget.** The worker loop ceiling (`_MAX_TOOL_EXECUTIONS`, 9.1) is the hard per-turn model-call cap (conservative default). Scheduler tool-turns carry a 5.2 `cost` weight so the daily budget bounds worst-case self-driven spend. Owner turns aren't budget-gated (5.2 design) but are loop-ceiling-bounded. No new $/token accounting (that needs broker token detail — deferred).

### Architecture constraints

- **Core stays LLM-free (import-linter).** Everything new in `core/` uses stdlib only (`resource`, `shutil`, `sqlite3`, `keyword`, `ast`). The gate + `run_shell` are subprocesses, never imports.
- **Core is the sole writer of the workspace tool dirs (AD-5)** — incl. the new `tools-quarantine/`. The worker only READS/imports `tools/`.
- **Fail-soft + fork-no-accumulation (AD-8/AD-3).** Every new guard degrades, never crashes a turn; the per-turn fork makes RLIMIT_AS a clean per-turn bound.
- **Additive wire only (AD-13).** `Result.tool_failures` is an optional default-`()` field — no `SCHEMA_VERSION` bump.

### OUT OF SCOPE — defer to Story 9.6 (tool-policy hardening)

These stay in `deferred-work.md` (9.2/9.3 review sections); do NOT pull them into 9.5:
- `http_get` SSRF-on-redirect blocking (`follow_redirects=True` reaches metadata/localhost)
- `http_get` streaming / pre-read byte cap (buffers full body before `_cap`)
- `git` subcommand allowlist (`clone`/`-c core.sshCommand`/`--upload-pack`)
- `run_shell` process-group / `start_new_session=True` cleanup (orphaned `&` children)
- `_deny_sensitive` credential-blocklist expansion (`.pem`/`.key`/`id_rsa`/`.env.bak`)

These are each a policy surface behind the 9.3 owner-approval gate; grouping them into a focused 9.6 keeps 9.5 about the Pi-safety pillars.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 9.5: Safety hardening] — the two AC groups (quarantine; resource caps + credit gating)
- [Source: _bmad-output/planning-artifacts/epic-9-self-coding-design-2026-06-21.md#9.5 — Safety hardening (woven through)] — quarantine (faces-registry pattern), RLIMIT caps, 5.2 credit reuse, loop ceiling as a safety control
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — the 9.2/9.4 review defers folded in here (caps, prune scheduling, gate/keyword/slug/dynamic-import/op-cap cleanup) and the 9.2/9.3 policy defers pushed to 9.6
- [Source: shelldon/core/budget.py + shelldon/core/dispatch.py] — the 5.2 `cost`-weighted daily-turn budget seam reused for AC3
- [Source: shelldon/core/history.py#learnings] — the atomic-UPSERT pattern to mirror for `tool_health`; the orphaned `prune_expired_*` methods to schedule

### Project Structure Notes

- New file: none required (all changes are additive to existing modules); `tests/test_resource_caps.py` is the one new test file.
- `tools-quarantine/` is created lazily by `selfcode.quarantine` (and may be pre-created in `app.py` alongside the 9.4 live/staging dirs — optional, mirror that pattern).
- No broker/transport/plugin changes — the broker seam and the agentic loop shape are unchanged (AC5).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Dev Story workflow)

### Debug Log References

- `uv run pytest -q` → 694 passed, 3 skipped (pre-existing FTS5-absent sims), 7 deselected (`-m live`). Baseline 675 collected; +22 new test cases.
- `uv run lint-imports` → 3 contracts KEPT (core LLM-free held — `core/limits.py` imports only stdlib `resource`).
- `uv sync --locked` → 0 changes (33 packages). All new code is stdlib (`resource`/`keyword`/`shutil`/`sqlite3`/`ast`).

### Completion Notes List

- **Quarantine (AC1) — worker DETECTS, core DECIDES + MOVES.** New `Result.tool_failures` (additive `()` default, no SCHEMA bump) carries the names of self-coded tools that failed this turn: discovery import-skips (surfaced via a new `skipped`/`import_failures` out-param threaded forkserver→`run_worker`) + run-failures (a new `self_coded` flag on `ToolSpec` lets the loop's `_record_tool_failure` attribute an `ok=False` to a self-coded tool, never a built-in). Core's `_handle_tool_failures` strikes each in the new `tool_health` sqlite table (atomic UPSERT like `learnings`) and, at `_QUARANTINE_STRIKE_THRESHOLD=3`, moves the live module to `tools-quarantine/` via `selfcode.quarantine` (sole-writer + move, AD-5). Runs whether or not the turn succeeded; fully fail-soft.
- **Resource caps (AC2) — new `core/limits.py` (stdlib `resource` only, LLM-free).** `apply_resource_caps()` sets `RLIMIT_AS` (default 1 GiB — a gross-runaway catcher well above the worker's ~244MB-RSS peak; systemd `MemoryMax=400M` is the hard physical backstop) + `RLIMIT_CPU` (default 30 CPU-s, above the 25s loop ceiling). Called in the fork child before `asyncio.run(run_worker)` (bounds python_eval + FREE self-coded tools + the loop). `resource_cap_preexec()` is wired as the `preexec_fn` on `_run_subprocess` (run_shell/git) and the gate `pytest` subprocess so children can't escape. Each setrlimit is guarded (unsupported RLIMIT / over-hard-limit → log + continue); soft never raised above the inherited hard limit. SIGALRM (9.2) kept as the layer RLIMIT can't cover. Tests assert limits are SET (injected recorder / inspected `preexec_fn` kwarg), not kill behavior (cross-platform).
- **Credit gating (AC3) — pure 5.2 reuse, no new code.** Documented the bounded-spend invariant on `_MAX_TOOL_EXECUTIONS` (worst-case self-driven spend = `daily_turn_budget` × loop-ceiling model-calls/day). The cost-weighted daily-budget debit is the UNCHANGED 5.2 path — already covered by `test_turn_dispatch.py::test_cost_weight_spends_multiple_budget_units` + `test_dream_dispatches_when_learnings_pending_spends_three`, so no redundant test was added (surgical). Did NOT bump dream `cost` (a behavior/frequency change out of the safety intent — left a note that tool-heavy scheduler turns could raise cost later).
- **Cheap cleanup (AC4).** Prune REFLEX scheduler job (`prune`, hourly) now calls `prune_expired_approvals` + `prune_expired_promotions` (the orphaned methods got a call site). `run_gate` kills + reaps its subprocess on `CancelledError` (a `BaseException` branch → re-raises). `_safe_tool_name` suffixes a Python keyword/soft-keyword stem (`class`→`class_tool`) so the module stays importable. `stage` discards the prior staged pair before writing (no stale `test_<stem>.py`) + warns on overwrite. `_forbidden_import` now also catches `__import__("…")`/`importlib.import_module("…")` literals in the forbidden set (and logs a non-literal dynamic arg as unverifiable). `_handle_result` searches for `ProposeTool` within the `MAX_PROPOSED_OPS`-capped slice + warns on >1. `promote` logs an info line when replacing an existing live tool.
- **Spine (AC5).** import-linter 3 KEPT; `core/limits.py` is stdlib-only and shared by worker (worker→core) + core; core stays the sole writer of the tool dirs incl. `tools-quarantine/`. The OUT-OF-SCOPE 9.6 items (SSRF, http_get streaming, git allowlist, run_shell process-group, cred-blocklist) were NOT touched — still in `deferred-work.md`.

### File List

- shelldon/contracts/__init__.py (UPDATE — `Result.tool_failures` additive field)
- shelldon/core/limits.py (NEW — RLIMIT caps: `apply_resource_caps` + `resource_cap_preexec`)
- shelldon/core/selfcode.py (UPDATE — `quarantine_dir`/`quarantine`, gate `CancelledError` cleanup + `preexec_fn`, keyword guard, stage hygiene, dynamic-import detection, promote audit log)
- shelldon/core/history.py (UPDATE — `tool_health` table + `record_tool_failure`/`tool_strikes`)
- shelldon/core/runtime.py (UPDATE — `_handle_tool_failures` + quarantine threshold, `prune` reflex job + `_run_prune_job`, `ProposeTool` op-cap + multi-propose warning)
- shelldon/worker/tools.py (UPDATE — `self_coded` flag, discovery `skipped` out-param, `build_tool_registry(import_failures=)`, `_run_subprocess` `preexec_fn`)
- shelldon/worker/worker.py (UPDATE — `_record_tool_failure`, `failures` threaded through the loop, `import_failures` seed + `Result` replace, loop-ceiling bounded-spend doc)
- shelldon/worker/forkserver.py (UPDATE — `apply_resource_caps()` in the fork child + `import_failures` threading)
- tests/test_resource_caps.py (NEW)
- tests/test_selfcode.py (UPDATE — quarantine, gate-cancel, keyword guard, stage hygiene, dynamic-import)
- tests/test_self_coded_discovery.py (UPDATE — self_coded flag, skip surfacing, import_failures, `_record_tool_failure`)
- tests/test_selfcode_flow.py (UPDATE — repeated-failure quarantine, prune job)
- tests/test_history.py (UPDATE — tool_health)
- tests/test_contracts_roundtrip.py (UPDATE — `Result.tool_failures`)

### Change Log

- 2026-06-22 — Review findings addressed: 1 [Decision] (AC3 proactive cost=1 — documented rationale, option B) + 4 [Patch] fixes (RLIMIT caps moved before tool discovery; quarantine idempotency guard; run_gate reap-on-cancel hardened against a second cancel; prune_expired_promotions now discards leaked staged files). 4 [Defer] accepted (spec-bounded). +2 tests (696 pass).
- 2026-06-22 — Story 9.5 implemented (safety hardening). Three pillars: QUARANTINE (worker reports failing self-coded tools on `Result.tool_failures` → core strikes in new `tool_health` table → moves to `tools-quarantine/` at 3 strikes); RLIMIT CAPS (new `core/limits.py`; RLIMIT_AS/RLIMIT_CPU on the worker fork + `preexec_fn` on run_shell/gate subprocesses; closes the 9.2 cap defer); CREDIT GATING (loop ceiling × unchanged 5.2 cost-weighted budget, documented). PLUS cheap cleanup: prune scheduler job, gate CancelledError cleanup, keyword/slug guards, dynamic-import detection, ProposeTool op-cap fix, promote audit log. `Result.tool_failures` additive (no SCHEMA bump). Network/shell policy items deferred to a future 9.6. 22 new test cases (694 pass), import-linter 3 KEPT, 0 new deps. Status → review.

### Review Findings

- [x] [Review][Decision] AC3: `proactive` job `cost=1` does not satisfy "cost ≥ worst-case loop multiplier" — RESOLVED via option B (documented rationale). `cost=1` is intentional: a proactive turn is a self-initiated MUSING (built from live mood), it doesn't run tool tasks, so it virtually never invokes the loop. The HARD runaway-spend bound does not depend on this cost — `_MAX_TOOL_EXECUTIONS` caps model-calls PER TURN regardless of cost, so worst-case spend = daily_turn_budget × ceiling no matter any turn's cost; `cost` rations TURNS not calls, and weighting proactive at 6 would gut its frequency (~2/day) for a worst case the ceiling already bounds. Added the rationale as a comment on the proactive `Job` registration in `runtime.py`. (Did NOT raise the cost — a behavior/frequency regression for a cosmetic.)
- [x] [Review][Patch] RLIMIT caps applied after `build_tool_registry` — import-time code in self-coded tools runs uncapped [shelldon/worker/forkserver.py] — FIXED: `apply_resource_caps()` now runs BEFORE `build_tool_registry()` so discovery's per-module `exec_module` (import-time code) runs under the caps.
- [x] [Review][Patch] `quarantine()` has no idempotency guard [shelldon/core/selfcode.py] — FIXED: `quarantine` returns False quietly when the live module is already gone (no misleading "failed" warning on a repeat strike). New test `test_quarantine_is_idempotent`. (The staged-files-leak half is the separate prune patch below.)
- [x] [Review][Patch] `await proc.wait()` inside `except BaseException` branch of `run_gate` can itself raise `CancelledError`, suppressing subprocess reap [shelldon/core/selfcode.py] — FIXED: the reap `await proc.wait()` is wrapped in `try/except BaseException: pass` (the kill is synchronous, so the process is dead regardless) before re-raising the original. New test `test_run_gate_cancel_reaps_even_if_wait_raises`.
- [x] [Review][Patch] `prune_expired_promotions` deletes only the DB row — staged files leak [shelldon/core/history.py, shelldon/core/runtime.py] — FIXED: `prune_expired_promotions` now RETURNS the pruned `tool_name`s (sqlite-only, no FS); `_run_prune_job` discards each pruned tool's staged pair (core = sole FS writer, AD-5). Tests updated (`test_prune_expired_promotions` asserts the returned names; `test_prune_job_drops_expired` asserts the staged files are gone).

- [x] [Review][Defer] Dynamic-import detection misses aliased `importlib` forms (`import importlib as il; il.import_module("anthropic")`) — spec-accepted: non-literal/alias forms are "unverifiable", owner-approval is the backstop; spec says defense-in-depth only [shelldon/core/selfcode.py] — deferred, pre-existing design limit per AC4 spec
- [x] [Review][Defer] `resource_cap_preexec` macOS: `asyncio.create_subprocess_exec` `preexec_fn` deprecated in Python 3.12+ and RLIMIT_AS not enforced on macOS — Linux/Pi is the explicit enforcement target per spec; dev env (macOS) is expected to differ [shelldon/core/limits.py] — deferred, Linux-only enforcement is spec intent
- [x] [Review][Defer] Strike count never reset after manual tool restore — after owner manually moves a tool back from `tools-quarantine/`, `tool_health` row still has `strikes=3`, so first failure triggers immediate re-quarantine — auto-rehabilitation is explicitly out of scope per AC1 spec; manual restore = owner responsibility [shelldon/core/history.py] — deferred, auto-rehab out of scope per spec
- [x] [Review][Defer] `_safe_tool_name` keyword suffix (`class` → `class_tool`) could collide with an existing tool of that name — `stage()` already handles overwrites with a log warning; acceptable design behavior [shelldon/core/selfcode.py] — deferred, handled by existing stage() overwrite guard
