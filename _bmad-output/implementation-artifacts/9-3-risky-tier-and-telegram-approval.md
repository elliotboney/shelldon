---
baseline_commit: ec296f9
---
# Story 9.3: Risky tier + Telegram approval

Status: done

## Story

As the owner,
I want risky actions (writing files, running shell/git, network) to wait for my approval over Telegram,
so that the pet's live coding can't change anything important without my say-so.

## Scope decisions (locked with Elliot 2026-06-21)

1. **Full scope** — ship the approval machinery AND all four RISKY tools (`write_file`, `run_shell`, `http_get`, `git`) in this story.
2. **Approval wire = additive fields** on the existing `OutboundMessage`/`InboundMessage` (AD-13 non-breaking) — NOT new `MsgKind`s. The bus stays transport-agnostic; Telegram renders the approval as an inline keyboard, CLI could render it as text. No `ROUTING_TABLE` / `_KIND_FOR_BODY` / `SCHEMA_VERSION` changes.

## Acceptance Criteria

### AC1 — A RISKY tool call pauses the turn and parks resumable state

**Given** the agentic loop (9.1) and a registered RISKY-tier tool
**When** the model requests a RISKY tool call
**Then** the worker does NOT execute it — it ends the turn emitting a `RequestToolApproval(call, summary, messages)` proposed-op plus a user-facing `Result.payload` note ("I'd like to run X — approve?"), and core persists the pending agent state (the running `messages` + the pending `ToolCall`) to sqlite keyed by `turn_id` with an `expires_at`
**And** the worker NEVER blocks waiting on a human — it dies normally (fork reaped, arbiter slot freed); the approval waits out-of-band in sqlite

### AC2 — Telegram surfaces the approval and renders tool output safely

**Given** a parked approval
**When** core sends the approval request outbound (`OutboundMessage(text=summary, approval_turn_id=turn_id)`)
**Then** the Telegram transport renders it with an inline Approve/Deny keyboard (callback buttons carrying the `turn_id`), sends with `parse_mode` (HTML, with `<pre>` for tool output blocks), answers the `callback_query` to clear the client spinner, and registers a command set via `setMyCommands` on startup
**And** the existing plain-reply path (no `approval_turn_id`) is unchanged (Markdown→plain fallback from 8.2 still applies)

### AC3 — A tap resumes a fresh worker that finishes the turn

**Given** a parked approval
**When** the owner taps Approve or Deny (a `callback_query` arrives)
**Then** the transport emits `InboundMessage(approval_turn_id=turn_id, approved=<bool>)`; core takes the parked state from sqlite and spawns a FRESH worker that resumes the loop — on Approve it executes the pending call, on Deny it skips it (feeding back a "denied by owner" `ToolResult`), appends the `ToolResult`, and continues the bounded loop to a final text reply that reaches the owner as a normal turn
**And** a resumed loop that hits ANOTHER RISKY call parks again (the flow is re-entrant)

### AC4 — Expired / unknown approvals fail closed

**Given** a parked approval older than its TTL (default 1h), or a decision for an unknown/already-consumed `turn_id`
**When** the decision arrives
**Then** it is dropped (the pending call NEVER executes), logged, and the owner gets a brief "that approval expired / is no longer pending" note — never a crash, never a stale execution

### AC5 — The four RISKY tools, gated and worker-side

**Given** the RISKY tier
**When** an approved RISKY tool runs in the resumed worker
**Then** `write_file` (workspace-jailed, reusing the 9.2 jail + vault/.env denial), `run_shell` (subprocess in the workspace cwd), `http_get` (plain GET via the already-present `httpx`, no credentials), and `git` (subprocess `git` in the workspace) each execute and feed a `ToolResult` back
**And** every spine invariant holds: `core/` imports no LLM/provider code (import-linter 3 contracts KEPT), the broker executes no tools (it only shuttles), the loop still runs inside ONE fork per turn (resume = a NEW fork from persisted state, AD-3), core is the sole sqlite writer (AD-5), and tool errors fail soft (`ToolResult(ok=False)`, never crash)

### AC6 — Backwards compatibility + boundary gate

**Given** the additive contract fields and the new tools
**When** 9.3 lands
**Then** a turn with no RISKY call behaves exactly as 9.1/9.2 (single round-trip or all-FREE loop), the new `OutboundMessage`/`InboundMessage` fields default such that old payloads still decode (no `SCHEMA_VERSION` bump), and the CLI transport still works (it ignores/!textually-handles approval fields)
**And** all existing tests pass (601+), import-linter 3 contracts green, `uv sync --locked` 0 new deps (`httpx` already present; `subprocess`/`pathlib`/`msgspec` stdlib/existing)

---

## Tasks / Subtasks

- [x] **Task 1 — Contracts: approval op + additive message fields** (AC1, AC2, AC3, AC6)
  - [x] `shelldon/contracts/__init__.py`: add `RequestToolApproval(msgspec.Struct, frozen, tag="request_tool_approval", forbid_unknown_fields)` with `call: ToolCall`, `summary: str`, `messages: tuple[Message, ...]`. Add it to the `ProposedOp` union and `__all__`.
  - [x] Extend `OutboundMessage`: add `approval_turn_id: str | None = None` (additive default — AC6).
  - [x] Extend `InboundMessage`: add `approval_turn_id: str | None = None` and `approved: bool | None = None` (additive defaults). When `approval_turn_id` is set, the message is an approval DECISION, not chat text.
  - [x] Do NOT touch `MsgKind`, `ROUTING_TABLE`, `_KIND_FOR_BODY`, `SCHEMA_VERSION` (decision 2 — additive only). Verify the existing msgspec round-trip tests still pass.

- [x] **Task 2 — `worker/tools.py`: the four RISKY tools + tier-aware registry** (AC5)
  - [x] `write_file(path, content, *, workspace_root, memory_root)` — RISKY. Reuse `_resolve_in_jail` + `_deny_sensitive`; create parent dirs within the jail; write text; return a short confirmation. (This is the FIRST writer tool — the 9.2 read-only invariant is intentionally lifted ONLY for this gated tool.)
  - [x] `run_shell(command, *, workspace_root)` — RISKY. `subprocess.run(command, shell=True, cwd=workspace_root, capture_output=True, text=True, timeout=...)`; return combined stdout/stderr + exit code (truncated to a cap like `_MAX_EVAL_OUTPUT_CHARS`). Best-effort time bound now; hard CPU/mem caps are Story 9.5.
  - [x] `http_get(url, *, ...)` — RISKY. `httpx.get(url, timeout=...)` (httpx already a dep, lazily imported); return status + capped body. NO credentials (NFR9: a credentialed API tool stays broker-side/deferred per design §3). Reject non-`http(s)` schemes.
  - [x] `git(args, *, workspace_root)` — RISKY. `subprocess.run(["git", *shlex.split(args)], cwd=workspace_root, capture_output=True, text=True, timeout=...)`; return output capped.
  - [x] Register all four in `build_tool_registry` with `tier=ToolTier.RISKY` and JSON-schemas; bind `workspace_root`/`memory_root` via `functools.partial` as in 9.2.
  - [x] Add `summarize_call(call: ToolCall, spec: ToolSpec) -> str` (or similar) producing the human approval summary (e.g. `run_shell: rm -rf build/`).

- [x] **Task 3 — Worker loop: pause on RISKY, resume entry** (AC1, AC3, AC5)
  - [x] `worker/worker.py::_agentic_loop`: when a `Completion.tool_calls` contains a RISKY-tier call, STOP before executing it — return a `Result(ok=True, payload=<user note>, proposed_ops=[RequestToolApproval(call, summary, messages)])`. (Execute any FREE calls that precede it? Keep it simple: pause on the FIRST RISKY call in the completion; do not execute the rest this round — document this.)
  - [x] Tier lookup: the worker has the `registry` (`dict[str, ToolSpec]`); `registry[call.name].tier == ToolTier.RISKY`. An unknown tool stays the 9.1 fail-closed path.
  - [x] `run_worker(..., resume=None)`: a new optional `resume` (a small struct: restored `messages`, the pending `ToolCall`, `approved: bool`). When set, SKIP prompt assembly; rebuild `messages`; if `approved` execute the pending call else synthesize `ToolResult(ok=False, content="denied by owner")`; append it; re-enter `_agentic_loop` from there to a final reply (or another RISKY pause).
  - [x] Preserve `_MAX_TOOL_EXECUTIONS` / 25s budget across resume (a resume is a fresh budget — a fresh fork — which is fine; note it).

- [x] **Task 4 — sqlite: park / take / expire pending approvals** (AC1, AC4)
  - [x] `shelldon/core/history.py`: add a `pending_approvals` table to `_SCHEMA` — `(turn_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state_blob BLOB NOT NULL)` (CREATE TABLE IF NOT EXISTS, same additive convention as `learnings`).
  - [x] `HistoryStore.park_approval(turn_id, state_blob, now, ttl_seconds)` — INSERT (one commit). `state_blob` = `msgspec.msgpack.encode((messages, call))` produced by core.
  - [x] `HistoryStore.take_approval(turn_id, now) -> bytes | None` — atomically read+DELETE a row whose `expires_at > now`; return the blob, or None if absent/expired (expired rows: DELETE + log + return None). One commit.
  - [x] `HistoryStore.prune_expired_approvals(now)` — best-effort housekeeping (optional, called on take).

- [x] **Task 5 — Core: route the approval op + the decision + spawn resume** (AC1, AC3, AC4)
  - [x] `core/runtime.py::_apply_proposed_ops`: add a `RequestToolApproval` branch — encode `(op.messages, op.call)` and `history.park_approval(turn_id, blob, now, ttl)`. (The user-facing note already went out via the normal reply path; set its `approval_turn_id` — see next.)
  - [x] When the result carried a `RequestToolApproval`, core's reply for this turn must set `approval_turn_id=turn_id` on the `OutboundMessage` so the transport renders buttons. (Plumb a flag from `_handle_result` → `_send_reply`, or send the approval outbound from the op branch. Keep the plain-reply path unchanged when there's no approval op.)
  - [x] `core/runtime.py::run`: an `INBOUND_MSG` whose body has `approval_turn_id` set is a DECISION — route to `_handle_approval_decision(turn_id, approved)` instead of `arbiter.submit`.
  - [x] `_handle_approval_decision`: `take_approval(turn_id)`; if None → send the "expired/not pending" note (AC4). Else decode the blob and start a RESUME turn via the spawner (respects the ≤1 fence/arbiter — see Dev Notes on admission), which finishes through the normal `_handle_result` path.
  - [x] `forkserver.py`: add `spawn_resume(turn_id, resume_state)` mirroring `spawn_turn` (same ≤1 guard, preload barrier, reap) but forking `run_worker(..., resume=resume_state)`. The `Spawner` protocol Core depends on gains `spawn_resume` (the in-process test spawner gets it too).

- [x] **Task 6 — Telegram transport: keyboards, callbacks, HTML, setMyCommands** (AC2, AC3)
  - [x] Generalize the transport seam so it can carry the approval metadata, not just text: `transport/runner.py` — the `OutboundSink` receives the `OutboundMessage` (or `(text, approval_turn_id)`), and the `InboundSource` can yield approval decisions as well as text. Update the CLI adapter (`transport/cli.py`) to the new seam (text in/out unchanged in behavior; it may render an approval as a text prompt + accept `/approve`/`/deny`, or simply pass-through — keep CLI minimal).
  - [x] `transport/telegram.py`: when an outbound has `approval_turn_id`, send with an inline keyboard (`reply_markup.inline_keyboard` Approve/Deny, `callback_data=f"{turn_id}:approve"` / `:deny`) and `parse_mode="HTML"` (wrap tool-output blocks in `<pre>`); poll `callback_query` updates in `getUpdates`, `answerCallbackQuery` to clear the spinner, and emit the decision inbound. `setMyCommands` on startup (absorbs field-note item 5 — slash commands).
  - [x] Keep the 8.2 plain-reply behavior (Markdown→plain fallback) for non-approval messages.

- [x] **Task 7 — Tests** (AC1–AC6)
  - [x] `tests/test_risky_approval.py` (NEW) — scripted fake provider/broker over the real bus (extend the 9.1 harness): RISKY call → assert the worker parks (Result carries `RequestToolApproval`, no execution), core writes the sqlite row + sends an outbound with `approval_turn_id`; Approve decision → fresh worker executes + final reply; Deny → skipped + "denied" fed back + final reply; expired/unknown decision → dropped + note (AC4); re-entrant (resume hits another RISKY) → parks again.
  - [x] `tests/test_risky_tools.py` (NEW) — unit-test each tool over `tmp_path`: `write_file` writes within jail + rejects escape/vault/.env; `run_shell` returns output + respects cwd + timeout; `http_get` (fake client) returns status/body + rejects non-http schemes; `git` (in a tmp repo or faked subprocess) returns output. All fail-soft on error.
  - [x] `tests/test_telegram.py` (UPDATE) — inline-keyboard render for an approval outbound; `callback_query` → decision inbound; `setMyCommands` called; HTML/`<pre>` for tool output; plain reply path still works.
  - [x] `tests/test_history.py` (UPDATE) — park/take/expire pending_approvals.
  - [x] Contracts round-trip test for `RequestToolApproval` + the additive message fields.
  - [x] Boundary gate: `uv run pytest -q` → all pass (601+ baseline + new); `uv run lint-imports` → 3 KEPT; `uv sync --locked` → 0 new deps.

---

## Dev Notes

### What 9.1 + 9.2 already built (read first — the foundation)

- **`worker/tools.py`** (read fully) — `ToolSpec(name, description, params_schema, tier, fn)`, `execute_tool` (catches ALL exceptions → `ToolResult(ok=False)`; **filters `call.args` to the schema** — so a RISKY tool's bound kwargs can't be model-injected), `build_tool_registry(workspace_root, memory_root)`, `_resolve_in_jail`, `_deny_sensitive`, `_python_eval`/`_assert_eval_safe`. RISKY tools reuse `_resolve_in_jail`/`_deny_sensitive` and register with `tier=ToolTier.RISKY`.
- **`worker/worker.py::_agentic_loop`** (read fully) — builds `messages: tuple[Message,...]`, sends `Job(tools, messages)`, reads `Completion`, executes FREE tools via `execute_tool`, appends `Message(role="tool", ...)`, loops; capped at `_MAX_TOOL_EXECUTIONS=6` within the 25s budget. 9.3 adds: a tier check that PAUSES on RISKY, and a `resume` entry. `tool_registry` is the injectable seam (forkserver passes `build_tool_registry()`).
- **`contracts/__init__.py`** — `ToolTier.FREE/RISKY` (RISKY defined in 9.1, ENFORCED here for the first time), `ToolCall`, `ToolResult`, `ToolDefinition`, `Message`, `ProposedOp` union, `Job.tools/messages`, `Completion.tool_calls`. All additive-field discipline (no SCHEMA_VERSION bump) is established.

### Architecture constraints (mandatory — spine invariants)

- **Core stays LLM-free (AD-1, import-linter).** The loop + tools live in the worker. Core only persists/routes approval state. NO provider SDK in core.
- **Broker executes nothing (AD-2).** The broker still only normalizes provider tool-call format and shuttles. RISKY tools run in the (resumed) worker.
- **Fork = no accumulation (AD-3/AD-9).** The worker still forks-per-turn-and-dies. A RISKY pause ENDS the turn (the worker dies); the resume is a NEW fork from sqlite state. The worker NEVER blocks waiting on the human (that's the whole 2-phase point — design §3 "the central tension").
- **Single-writer (AD-5).** Core is the sole sqlite writer — it parks/takes approvals. `write_file` writes ONLY inside the workspace jail (never memory/vault/state).
- **Fail-soft.** Tool errors → `ToolResult(ok=False)`. Expired/unknown approval → dropped + note (AC4). A bad decision frame must never crash core.
- **Coherent timeout W<R<T (5.0).** Each fork (initial and resume) is independently bounded by the 25s loop ceiling. The human think-time happens BETWEEN forks (no worker is alive during it), so the timeout invariant is untouched.

### Turn lifecycle (core/runtime.py — exact integration points)

- `run()` loop: `INBOUND_MSG` → `arbiter.submit` → `_start_turn(prompt)` → `spawner.spawn_turn(turn_id, prompt)`. `RESULT` → `_handle_result` → `fence.accept` → `_send_reply` → `_apply_proposed_ops` → `_record_turn` → `_await_reap` → `arbiter.complete` (slot release). (lines ~329–422.)
- **Decision routing:** in `run()`, branch BEFORE `arbiter.submit`: `if env.body.approval_turn_id is not None: await self._handle_approval_decision(...)`. A decision is NOT owner chat — it must not coalesce/dedup through the arbiter.
- **Parking:** `_apply_proposed_ops` (lines ~? — isinstance dispatch: `AddFace`/`CaptureLearning`/`ResolveLearning`/else→`apply_memory_op`) gains a `RequestToolApproval` branch → `history.park_approval(self._current_turn_id, blob, now, ttl)`. NOTE: `_apply_proposed_ops` runs AFTER `_send_reply`; the reply for an approval turn must carry `approval_turn_id`. Simplest: in `_handle_result`, detect a `RequestToolApproval` op and pass its `turn_id` into the reply send (so the SAME outbound that carries the summary text also carries `approval_turn_id`). Keep the plain path (no approval op) byte-for-byte unchanged.
- **Resume admission (≤1 guard):** a resume IS a turn (needs fence + reap + timeout). Route `_handle_approval_decision` through the same machinery as `_start_turn` but calling `spawner.spawn_resume(...)`. If a worker is already in flight, the resume must wait — reuse the arbiter/fence discipline (the decision can re-defer; the sqlite row is only `take`n once admission is certain — `take_approval` AFTER the slot is free, or re-park on busy). Document the chosen approach; the safe default: only `take_approval` when about to spawn, and if busy, leave it parked and reply "busy, tap again in a sec" (single-owner — acceptable).

### sqlite (core/history.py)

Add a table mirroring the `learnings` additive pattern (CREATE TABLE IF NOT EXISTS in `_SCHEMA`). Core owns the writer (`HistoryStore`); workers never touch approvals (read-only handle is recall-only). The `state_blob` is `msgspec.msgpack.encode((messages, call))` — `Message`/`ToolCall` are msgspec structs, so this round-trips. Keep the blob bounded (messages ≤ 6 iterations); log if oversized.

### Transport seam change (the heaviest part — design with care)

`transport/runner.py` currently: `InboundSource = AsyncIterator[str]`, `OutboundSink = Callable[[str], Awaitable[None]]` — text only. 9.3 needs structured approval both ways. Generalize MINIMALLY:
- `_inbound_loop` already wraps each string into `InboundMessage(text=line)`. Let the inbound source yield either a `str` (chat) or an approval decision; build the `InboundMessage` with `approval_turn_id`/`approved` accordingly. Cleanest: the source yields `InboundMessage` objects (or a small tagged tuple) and the loop forwards them — update CLI + Telegram sources.
- `_outbound_loop` passes `env.body.text` to the sink. Change to pass the `OutboundMessage` (so the sink sees `approval_turn_id`). Update the CLI sink + Telegram sink signatures.
- This touches `cli.py` too — keep its behavior identical for plain text (an approval over CLI can render as "`<summary>` — reply /approve or /deny").

### Telegram specifics (transport/telegram.py)

- Inline keyboard: `reply_markup={"inline_keyboard": [[{"text":"✅ Approve","callback_data":f"{tid}:approve"},{"text":"❌ Deny","callback_data":f"{tid}:deny"}]]}` on the `sendMessage` for an approval outbound.
- `parse_mode="HTML"` for approval/tool-output messages; wrap tool output in `<pre>...</pre>` (HTML-escape the content first). The plain-reply path keeps the 8.2 Markdown→plain fallback.
- `getUpdates` must also surface `callback_query` updates (not just `message`): on one, parse `callback_data` → `(turn_id, decision)`, `answerCallbackQuery(callback_query_id)` to clear the spinner, and yield the decision inbound. Keep `allowed_users` gating on `callback_query.from.id` too.
- `setMyCommands` once on startup (absorbs field-note item 5). General `parse_mode` reply rendering already shipped (`6866f17`); this story owns the tool-output HTML/`<pre>`, keyboards, and callback routing.

### RISKY tools — safety notes

- `write_file` is the FIRST tool that writes — jail it HARD (reuse `_resolve_in_jail` + `_deny_sensitive`; never memory/vault/state). It is gated by approval, so the owner sees the path+content summary before it runs.
- `run_shell`/`git` use `subprocess.run` with `cwd=workspace_root`, `capture_output=True`, `text=True`, and a `timeout`. Output capped. Hard CPU/mem/process caps (RLIMIT) are explicitly Story 9.5 — note the best-effort bound here.
- `http_get` uses the already-present `httpx` (0 new deps), no credentials, http(s) only, response body capped. Credentialed API tools remain broker-side/deferred (design §3, NFR9).
- The approval gate is the safety boundary — these tools only ever run AFTER an explicit owner tap. The summary the owner sees must be faithful (show the actual command/path/url).

### No new dependencies

`httpx` (http_get) is already a dep (Telegram/transport use it). `subprocess`, `pathlib`, `shlex`, `msgspec`, `signal` are stdlib/existing. `uv sync --locked` must show 0 changes.

### What 9.4 / 9.5 build next (do NOT pull in)

- **9.4** persistent self-coded tools (`ProposeTool` → stage → CI gate → approve via THIS story's keyboard → fork-reimport). 9.3 only needs to leave the approval keyboard reusable.
- **9.5** hardening: RLIMIT CPU/mem caps for `run_shell`/`python_eval`, tool quarantine, cost/credit-tier gating for multi-call loops. 9.3 ships best-effort timeouts only.

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (dev-story workflow)

### Debug Log References

- Boundary gate green: `uv run pytest -q` → 627 passed, 3 skipped, 7 deselected (live); `uv run lint-imports` → 3 KEPT, 0 broken; `uv sync --locked` → 0 new deps.

### Completion Notes List

- **2-phase resumable flow, no blocking.** Worker pauses on the first RISKY call: it emits `RequestToolApproval(call, summary, messages)` + a user note and DIES (never blocks a human, AD-3). Core parks `(messages, call)` as a msgpack blob in a new `pending_approvals` sqlite table keyed by turn id. A tap (`InboundMessage(approval_turn_id, approved)`) routes — before the arbiter — to `_handle_approval_decision`, which takes the blob and spawns a FRESH worker (`spawn_resume`) that resolves the call (execute on approve / "denied by owner" ToolResult on deny), appends it, and continues the loop. Re-entrant (a resume can pause again).
- **Tool-protocol gotcha handled.** When a completion mixes FREE + RISKY calls, the worker executes the FREE prefix, keeps the assistant message to `[FREE prefix + the risky call]`, and parks — so on resume every `tool_use` block gets a `tool_result` (no provider 400). Calls after the first risky one are dropped (the model re-requests).
- **Resume admission vs the ≤1 guard.** A resume IS a turn (needs fence/arbiter/reap). `_handle_approval_decision` only proceeds when `arbiter.is_idle`; if busy it leaves the approval PARKED and replies "tap again in a moment" (the sqlite row is consumed only when about to spawn). Avoids the arbiter-coalescing-a-resume-marker bug.
- **Core stayed worker-decoupled.** `spawn_resume(turn_id, messages, call, approved)` takes plain contract data; the forkserver builds the worker-only `ResumeState`. Core imports nothing from `worker/`. Resume state crosses the fork by INHERITANCE (in-process), never the bus.
- **Transport seam generalized minimally.** `run_transport` gained an optional `on_approval_request(text, turn_id)` sink and the inbound source may now yield an `InboundMessage` (a decision) as well as a `str` — so CLI + all 6 existing `sink(text)` test harnesses are UNCHANGED. Telegram provides the keyboard sink + callback handling; CLI renders an approval as plain text (no keyboard).
- **Telegram:** inline Approve/Deny keyboard (callback_data carries the turn id), HTML parse_mode with code-spans → `<pre>` (AC2), `answerCallbackQuery` to clear the spinner, `setMyCommands` on startup (field-note item 5). Plain-reply path keeps the 8.2 Markdown→plain fallback.
- **0 new deps:** `httpx` (http_get) already present + lazily imported inside the tool (module-level imports stay SDK-clean → import-linter 3 KEPT); `subprocess`/`shlex`/`signal`/`msgspec` stdlib/existing.
- **Best-effort caps only:** `run_shell`/`git`/`http_get` are wall-clock bounded (`_RISKY_TIMEOUT_S`) + output-capped; hard RLIMIT CPU/mem caps are Story 9.5.

### File List

- shelldon/contracts/__init__.py (UPDATE — RequestToolApproval op + ProposedOp union moved below Message; OutboundMessage.approval_turn_id; InboundMessage.approval_turn_id/approved; __all__)
- shelldon/worker/tools.py (UPDATE — write_file/run_shell/http_get/git RISKY tools + _run_subprocess/_cap/summarize_call + register; _RISKY_TIMEOUT_S/_MAX_TOOL_OUTPUT_CHARS)
- shelldon/worker/worker.py (UPDATE — ResumeState, RISKY pause in _agentic_loop, _resume_loop, run_worker resume param; _agentic_loop now takes messages)
- shelldon/worker/forkserver.py (UPDATE — spawn_resume + _default_resume_spawn + _os_fork_spawn resume kwarg)
- shelldon/core/runtime.py (UPDATE — approval-op park, decision routing, _handle_approval_decision, _start_resume_turn, _send_reply approval_turn_id)
- shelldon/core/history.py (UPDATE — pending_approvals table + park/take/prune; DEFAULT_APPROVAL_TTL_S)
- shelldon/transport/runner.py (UPDATE — on_approval_request sink + str|InboundMessage inbound)
- shelldon/transport/telegram.py (UPDATE — inline keyboards, callback_query, HTML/<pre> _to_html, setMyCommands, send_approval/_handle_callback/set_commands)
- tests/test_risky_approval.py (NEW — worker pause/resume + core park/decision/expiry, 6 tests)
- tests/test_risky_tools.py (NEW — the 4 tools, 9 tests)
- tests/test_telegram_transport.py (UPDATE — keyboard/callback/setMyCommands, 5 tests)
- tests/test_history.py (UPDATE — park/take/expire/prune, 4 tests)
- tests/test_contracts_roundtrip.py (UPDATE — RequestToolApproval + additive fields, 2 tests)

### Change Log

- 2026-06-21: Implemented Story 9.3 risky-tier + Telegram approval — RISKY tools (write_file/run_shell/http_get/git), worker pauses on a RISKY call emitting RequestToolApproval, core parks resumable state in a new sqlite pending_approvals table, owner taps over a Telegram inline keyboard, a fresh worker resumes (execute/deny) and finishes the turn. Additive contract fields (no SCHEMA_VERSION bump), transport seam gained an approval sink + structured inbound, expired approvals fail closed. 26 new tests; 627 pass / 3 import-linter contracts KEPT / 0 new deps.
- 2026-06-22: Addressed code review — 8 patches applied: malformed decision frame (approved=None) fails safe to deny + logs; `take_approval` decides expiry inside the txn before DELETE; empty-`turn_id` callback dropped; `http_get` rejects URL-embedded credentials (NFR9); `write_file` rejects oversized content (Pi disk); `inbound()` return type widened to `str | InboundMessage`; typing indicator shown after a tap; large parked-message blob logs a warning. 7 deferred (mostly 9.5 hardening: SSRF/streaming/process-groups/git-allowlist), 11 dismissed. +3 regression tests; 630 pass / 3 contracts KEPT / 0 new deps.

---

### Review Findings

_Code review 2026-06-21 — 3 layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor). 8 patches, 7 deferred, 11 dismissed._

#### Patches

- [x] [Review][Patch] `bool(env.body.approved)` coerces `None` → silent Deny with no log [shelldon/core/runtime.py:342] — `approved: bool | None`, so a malformed InboundMessage (approval_turn_id set, approved not filled) silently denies. Fix: `approved = bool(env.body.approved) if env.body.approved is not None else False` + a log.warning.
- [x] [Review][Patch] `take_approval`: DELETE committed before expiry check — should check first [shelldon/core/history.py:257] — SELECT+DELETE in one `with self._conn` block, then `if row["expires_at"] <= now_s` outside the transaction. Fix: move the expiry comparison inside the `with` block before issuing DELETE.
- [x] [Review][Patch] Empty `turn_id` from `:approve` callback data not guarded [shelldon/transport/telegram.py:~163] — `rpartition(":")` on `":approve"` gives `turn_id=""`. `take_approval("", now)` returns None → misleading "expired" note. Fix: `if not turn_id: return None` after rpartition.
- [x] [Review][Patch] `_http_get` allows embedded credentials via `http://user:pass@host` (violates NFR9) [shelldon/worker/tools.py:271] — scheme check passes, httpx sends credentials. Fix: `if urlparse(url).userinfo: raise ValueError("credentials in URL not allowed")`.
- [x] [Review][Patch] `_write_file` has no content size limit [shelldon/worker/tools.py:244] — model can write arbitrary-size content, risking disk exhaustion on Pi. Fix: cap at `_MAX_TOOL_OUTPUT_CHARS` and truncate (or reject with a clear error).
- [x] [Review][Patch] `TelegramChat.inbound()` return annotation is `AsyncIterator[str]` but yields `InboundMessage` objects [shelldon/transport/telegram.py] — breaks strict typing and typed consumers. Fix: update return type to `AsyncIterator[str | InboundMessage]` to match `InboundSource`.
- [x] [Review][Patch] No typing indicator shown after owner taps Approve/Deny [shelldon/transport/telegram.py:103] — `_handle_callback` does not call `_start_typing()`; the resumed worker can run seconds with no visual activity signal. Fix: call `self._start_typing()` in `_handle_callback` after setting `_chat_id`.
- [x] [Review][Patch] Messages blob unbounded — Dev Notes require log/cap at ≤6 iterations but no guard exists [shelldon/core/runtime.py:772] — each chained RISKY approval inflates the blob; no size warning or cap before msgpack encode. Fix: log a warning if `len(op.messages) > _MAX_TOOL_EXECUTIONS * 2`; hard cap can wait for 9.5.

#### Deferred

- [x] [Review][Defer] `_http_get` follows redirects without SSRF mitigation — deferred, 9.5 hardening scope [shelldon/worker/tools.py:278]
- [x] [Review][Defer] `_http_get` buffers full HTTP response before `_cap()` — OOM risk on 416MB Pi — deferred, 9.5 streaming scope [shelldon/worker/tools.py:278]
- [x] [Review][Defer] `prune_expired_approvals` is never scheduled — expired rows accumulate indefinitely — deferred, low urgency housekeeping [shelldon/core/history.py:263]
- [x] [Review][Defer] `_run_shell` can orphan background processes spawned via `&` — deferred, 9.5 process-group cleanup scope [shelldon/worker/tools.py:260]
- [x] [Review][Defer] `_git` lacks git subcommand allowlist — deferred, 9.5 hardening scope [shelldon/worker/tools.py:267]
- [x] [Review][Defer] `fence.open` raise in `_start_resume_turn` leaves arbiter slot stuck — deferred, extremely theoretical (fence.open never raises in practice), pre-existing pattern [shelldon/core/runtime.py:~430]
- [x] [Review][Defer] `<pre>` wraps all code spans, spec AC2 says "tool output blocks" — deferred, intentional per dev-agent completion notes (code-spans → `<pre>`) [shelldon/transport/telegram.py:41]
